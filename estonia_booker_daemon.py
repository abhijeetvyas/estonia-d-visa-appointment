#!/usr/bin/env python3
"""
Parked-Session Booker Daemon - Estonia D-Visa.

Implements `SPEC - Parked Session Booker Daemon.md` in full. Read that file
before touching this one; every requirement ID referenced in comments below
(R1-R42) is defined there, and `IMPLEMENTATION_CHANGELOG.md` maps every one
of them to where it's satisfied in this file.

ONE-PARAGRAPH SUMMARY (spec §4.1)
----------------------------------
Getting past the CAPTCHA gate costs ~20s, but the resulting session ("seat")
then survives for a working 20 minutes (measured ceiling 24, F5) and can
check real free-times for free (F6). So: pay the 20s once, early - as soon as
the monitor's trigger file shows D-visa appeared - then sit parked on the
calendar page and poll. The instant a slot appears, submit immediately. This
turns "detect, then spend ~25s clawing through a form while someone else
takes the slot" into "already sitting there, submit in ~1s".

NON-NEGOTIABLE SAFETY PROPERTY (R1-R4)
---------------------------------------
This process is completely separate from `estonia_visa_monitor.py` (untouched,
R1) and from the cron-driven detection cadence (R2). It has its own lock, own
log, own state files. Any crash, hang, or runaway behaviour in this daemon
must never be able to affect the monitor's alerting - if this process is
stopped or was never started, behaviour must be identical to today (R4). The
owner's stated requirement is that even total automation failure still means
a human gets the Telegram/call and can book by hand.

USAGE
-----
    venv/bin/python3 estonia_booker_daemon.py

Runs forever until SIGTERM/SIGINT (R40: finishes any in-flight booking, logs
shutdown, exits 0). See BOOKER_README.md for the systemd unit
(`estonia-booker-daemon.service`) and the full environment variable table.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

import estonia_booking_http as http_flow
from estonia_booking_http import (
    Seat,
    Slot,
    BookingHttpError,
    SeatDeadError,
    RECAPTCHA_SITEKEY,
    URL,
    check_service_offered,
    establish_seat,
    cache_applicant_on_seat,
    resolve_r17_refresh_dates,
    query_free_times,
    submit_booking,
)

# ---------------------------------------------------------------------------
# Configuration (spec §6.9 - every one env-overridable, defaults as spec'd)
# ---------------------------------------------------------------------------

EMBASSY_ID = os.environ.get("EMBASSY_ID", "40")
EMBASSY_NAME = os.environ.get("EMBASSY_NAME", "New Delhi")

SEAT_TTL_SECONDS = int(os.environ.get("SEAT_TTL_SECONDS", "1440"))          # R10: measured ceiling (F5)
SEAT_RENEW_AFTER_SECONDS = int(os.environ.get("SEAT_RENEW_AFTER_SECONDS", "1200"))  # R10: operating number
SLOT_POLL_INTERVAL_SECONDS = float(os.environ.get("SLOT_POLL_INTERVAL_SECONDS", "2"))  # R15
# Round-2 follow-up Fix 2: a new date appearing is an episode-scale event
# (minutes apart, F9), not a sub-second one - refreshing the date list on
# every slot poll (every ~0.5-2s) doubled request volume for no benefit.
# This is gated independently of SLOT_POLL_INTERVAL_SECONDS so the two
# cadences can be tuned separately; see maybe_refresh_dates().
DATE_REFRESH_INTERVAL_SECONDS = float(os.environ.get("DATE_REFRESH_INTERVAL_SECONDS", "10"))
# Round-3 follow-up Fix 8: a forced date-list refresh (triggered by a new
# trigger-file update while a seat is already live) runs at most this often,
# no matter how many trigger updates arrive - the monitor re-announcing the
# same episode every ~10s (estonia_visa_monitor_booker_hook.py writes the
# trigger file on EVERY detection tick, not once per episode) is not new
# information. See IMPLEMENTATION_CHANGELOG.md §9 for the incident this fixes.
FORCED_REFRESH_MIN_INTERVAL_SECONDS = float(os.environ.get("FORCED_REFRESH_MIN_INTERVAL_SECONDS", "30"))
# Round-3 Fix 7: how recent a trigger must be for "every date came back
# empty" to count as suspicious (as opposed to just... no slots existing
# right now, which is completely normal per R18/F10). See
# check_suspect_and_maybe_reacquire().
TRIGGER_FRESH_WINDOW_SECONDS = float(os.environ.get("TRIGGER_FRESH_WINDOW_SECONDS", "60"))
# Round-3 Fix 7: consecutive slot-poll cycles (every date returning zero
# slots) required, while a trigger is fresh, before the seat is considered
# suspect. At the default 2s poll cadence this is ~10s of corroborating
# evidence, not a single unlucky poll.
SUSPECT_AFTER_EMPTY_POLLS = int(os.environ.get("SUSPECT_AFTER_EMPTY_POLLS", "5"))
# Round-3 Fix 10: hard backstop - suspect-driven reacquisition fires at most
# once per this many seconds, REGARDLESS of cause. This is what turns a wrong
# heuristic into "costs one CAPTCHA every 5 minutes" instead of "costs one
# CAPTCHA every 10 seconds and never actually polls for a slot" (round 2's
# Fix 5 bug - see IMPLEMENTATION_CHANGELOG.md §9).
REACQUIRE_COOLDOWN_SECONDS = float(os.environ.get("REACQUIRE_COOLDOWN_SECONDS", "300"))
TOKEN_REFRESH_SECONDS = int(os.environ.get("TOKEN_REFRESH_SECONDS", "75"))   # R27
TOKEN_MAX_AGE_SECONDS = int(os.environ.get("TOKEN_MAX_AGE_SECONDS", "100"))  # R29
MAX_SOLVES_PER_HOUR = int(os.environ.get("MAX_SOLVES_PER_HOUR", "60"))      # R31
CAPSOLVER_LOW_BALANCE_USD = float(os.environ.get("CAPSOLVER_LOW_BALANCE_USD", "2.00"))  # R32

DAEMON_DRY_RUN = os.environ.get("DAEMON_DRY_RUN", "").lower() in ("1", "true", "yes")  # R26
DAEMON_SELF_DETECT = os.environ.get("DAEMON_SELF_DETECT", "").lower() in ("1", "true", "yes")  # R8, opt-in, default off

CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY")
CAPTCHA_PROVIDER = os.environ.get("CAPTCHA_PROVIDER", "capsolver").strip().lower()  # for on-demand fallback (R23/R30)

TRIGGER_POLL_INTERVAL_SECONDS = 0.5  # R7 (not env-overridable - it's a fixed spec requirement, not a tunable)
SELF_DETECT_INTERVAL_SECONDS = 10.0  # only used if DAEMON_SELF_DETECT=1
BALANCE_CHECK_INTERVAL_SECONDS = 15 * 60  # R32

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state"
TRIGGER_FILE = STATE_DIR / f"trigger_{EMBASSY_ID}.json"
# Deliberately the SAME booked-flag file used by the old Playwright booker
# (estonia_d_visa_booker.py's STATE_FILE convention) - R25 requires never
# booking twice, and that guarantee only holds if both bookers that could
# ever run against this embassy (old Playwright fallback, new daemon) check
# and set the same flag rather than maintaining independent, unsynced ideas
# of whether a booking already exists.
BOOKED_STATE_FILE = STATE_DIR / f"booker_state_{EMBASSY_ID}.json"
DAEMON_LOCK_FILE = Path(f"/tmp/estonia_booker_daemon_{EMBASSY_ID}.lock")  # R41

DAEMON_LOG_FILE = SCRIPT_DIR / "daemon.log"          # R33
POLLS_CSV_FILE = SCRIPT_DIR / "daemon_polls.csv"     # R35
DUMPS_DIR = SCRIPT_DIR / "daemon_dumps"              # R36

DUMMY_DATA_PATH = Path(os.environ.get("APPLICANT_DATA_DUMMY", SCRIPT_DIR / "applicant_data_dummy.json"))
REAL_DATA_PATH = Path(os.environ.get("APPLICANT_DATA_REAL", SCRIPT_DIR / "applicant_data_real.json"))

RUNTIME_LABEL = os.environ.get("RUNTIME_LABEL", "GCP")
RUNTIME_SIGNATURE = f"\n\n— Sent from {RUNTIME_LABEL} booker daemon"  # R38: reuse existing convention


# ---------------------------------------------------------------------------
# Logging (R33, R34) - own file, own format, independent of the monitor's
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def log_line(event: str, msg: str = "", elapsed_since_trigger: Optional[float] = None) -> None:
    """One UTC ISO-8601 timestamped line per event (R33), matching the
    existing `log_line()` style used throughout this project. `event` should
    be one of the R34 state-transition names (TRIGGER_RECEIVED,
    SEAT_ACQUIRING, ... RATE_LIMIT_HIT) for anything that's a state
    transition; free-form informational lines may use event="INFO"."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elapsed_part = f" elapsed_since_trigger={elapsed_since_trigger:.2f}s" if elapsed_since_trigger is not None else ""
    line = f"{ts} [{event}]{elapsed_part} {msg}".rstrip()
    with _log_lock:
        print(line, flush=True)
        try:
            with open(DAEMON_LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            # The daemon's own log failing to write must not crash the
            # daemon - stdout (captured by systemd/journald, R39) is the
            # fallback record in that case.
            pass


def dump_html(name: str, html: str) -> str:
    """R36: on any booking failure, dump the full response HTML and return
    the path so it can be referenced in the log line."""
    try:
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        path = DUMPS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)
    except Exception as e:
        return f"<dump failed: {e}>"


_csv_header_written = POLLS_CSV_FILE.exists() and POLLS_CSV_FILE.stat().st_size > 0


def append_poll_csv(seat_age_s: float, date: str, slot_count: int, slot_times: list, query_ms: float, seat_alive: bool) -> None:
    """R35: utc, seat_age_s, date, slot_count, slot_times, query_ms, seat_alive."""
    global _csv_header_written
    try:
        with open(POLLS_CSV_FILE, "a") as f:
            if not _csv_header_written:
                f.write("utc,seat_age_s,date,slot_count,slot_times,query_ms,seat_alive\n")
                _csv_header_written = True
            utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            times = "|".join(slot_times)
            f.write(f"{utc},{seat_age_s:.1f},{date},{slot_count},{times},{query_ms:.0f},{seat_alive}\n")
    except Exception as e:
        log_line("INFO", f"Failed to append poll CSV (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Telegram (R37) - fires only on the events listed in the spec, never routine polls
# ---------------------------------------------------------------------------

def _telegram_creds() -> tuple[str, list[str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("VISA_BOT")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("VISA_BOT_CHAT_ID")
    if not token or not chat_id_raw:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    chat_ids = [c.strip() for c in chat_id_raw.split(",") if c.strip()]
    if not chat_ids:
        raise RuntimeError("TELEGRAM_CHAT_ID is empty after parsing")
    return token, chat_ids


def send_telegram(message: str) -> None:
    try:
        token, chat_ids = _telegram_creds()
    except Exception as e:
        log_line("INFO", f"Telegram not configured, skipping send: {e}")
        return
    errors = []
    sent = 0
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
                timeout=30,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
    if sent == 0:
        log_line("INFO", f"Telegram send failed to ALL recipients: {'; '.join(errors)}")
    elif errors:
        log_line("INFO", f"Telegram partial failure: {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# Booked-flag state (shared with estonia_d_visa_booker.py, R24/R25)
# ---------------------------------------------------------------------------

def load_booked_state() -> dict:
    try:
        with open(BOOKED_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_booked_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(BOOKED_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_already_booked() -> bool:
    return bool(load_booked_state().get("booked"))


def mark_booked(details: dict) -> None:
    state = load_booked_state()
    state["booked"] = True
    state["booked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["booking_details"] = details
    state["booked_by"] = "estonia_booker_daemon"
    save_booked_state(state)
    log_line("BOOKING_SUCCESS", f"Persistent booked flag set: {details}")


# ---------------------------------------------------------------------------
# Applicant data
# ---------------------------------------------------------------------------

def load_applicant_data() -> dict:
    path = DUMMY_DATA_PATH if DAEMON_DRY_RUN else REAL_DATA_PATH
    if not path.exists():
        raise RuntimeError(f"Applicant data file not found: {path}")
    with open(path) as f:
        data = json.load(f)
    required = [
        "first_name", "surname", "date_of_birth", "phone",
        "email", "visa_application_number", "specifying_information",
    ]
    missing = [k for k in required if k not in data or not str(data[k]).strip()]
    if missing:
        raise RuntimeError(f"Applicant data missing keys: {missing}")
    return data


# ---------------------------------------------------------------------------
# Single-instance lock (R41)
# ---------------------------------------------------------------------------

def acquire_daemon_lock() -> bool:
    try:
        fd = os.open(str(DAEMON_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Stale-lock check: if the PID in the file is no longer running,
        # treat the lock as abandoned (e.g. a previous run crashed/was
        # killed -9 without cleanup) rather than refusing to start forever.
        try:
            content = DAEMON_LOCK_FILE.read_text().strip().splitlines()
            pid = int(content[0]) if content else None
            if pid is not None:
                try:
                    os.kill(pid, 0)
                    return False  # still alive
                except ProcessLookupError:
                    log_line("INFO", f"Removing stale daemon lock (pid {pid} no longer running)")
                    DAEMON_LOCK_FILE.unlink(missing_ok=True)
                    return acquire_daemon_lock()
        except Exception:
            pass
        return False


def release_daemon_lock() -> None:
    try:
        DAEMON_LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CAPTCHA solving
# ---------------------------------------------------------------------------
# R30: the token RESERVE (kept fresh while a seat is live) must use CapSolver
# alone, never CAPTCHA_PROVIDER=race - racing exists to cut latency variance
# for something being actively waited on, and nothing ever waits on a
# background reserve refresh, so racing there would double cost for zero
# benefit. `race` is reserved for the on-demand fallback path (R23), where a
# real booking attempt IS waiting and latency is everything.

class RateLimitExceeded(RuntimeError):
    pass


class SolveRateLimiter:
    """R31: cap solves at MAX_SOLVES_PER_HOUR. A simple rolling-window
    counter (deque of timestamps) rather than a fixed-bucket counter, so
    "per hour" means "in the trailing 3600s" rather than resetting on
    wall-clock hour boundaries."""

    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._alerted = False

    def check_and_record(self) -> None:
        with self._lock:
            now = time.time()
            while self._timestamps and now - self._timestamps[0] > 3600:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_hour:
                if not self._alerted:
                    log_line("RATE_LIMIT_HIT", f"{self.max_per_hour} solves/hour cap reached - pausing all solving")
                    send_telegram(
                        f"⚠️ Estonia D-visa booker daemon hit its CAPTCHA rate limit "
                        f"({self.max_per_hour}/hour) for {EMBASSY_NAME}. Solving paused; "
                        f"the monitor and any existing seat keep running.{RUNTIME_SIGNATURE}"
                    )
                    self._alerted = True
                raise RateLimitExceeded()
            self._timestamps.append(now)
            self._alerted = False


_rate_limiter = SolveRateLimiter(MAX_SOLVES_PER_HOUR)


def solve_capsolver(timeout: int = 120) -> str:
    if not CAPSOLVER_API_KEY:
        raise RuntimeError("CAPSOLVER_API_KEY not set")
    _rate_limiter.check_and_record()
    resp = requests.post(
        "https://api.capsolver.com/createTask",
        json={
            "clientKey": CAPSOLVER_API_KEY,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": URL,
                "websiteKey": RECAPTCHA_SITEKEY,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("errorId"):
        raise RuntimeError(f"CapSolver create task failed: {result}")
    task_id = result["taskId"]

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(3)
        poll = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id},
            timeout=30,
        )
        poll.raise_for_status()
        data = poll.json()
        if data.get("errorId"):
            raise RuntimeError(f"CapSolver error: {data}")
        if data.get("status") == "ready":
            return data["solution"]["gRecaptchaResponse"]
    raise RuntimeError("CapSolver timed out")


def solve_2captcha(timeout: int = 120) -> str:
    api_key = os.environ.get("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise RuntimeError("TWOCAPTCHA_API_KEY not set")
    _rate_limiter.check_and_record()
    resp = requests.post(
        "http://2captcha.com/in.php",
        data={"key": api_key, "method": "userrecaptcha", "googlekey": RECAPTCHA_SITEKEY, "pageurl": URL, "json": 1},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != 1:
        raise RuntimeError(f"2captcha create task failed: {result}")
    captcha_id = result["request"]

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        poll = requests.get(
            "http://2captcha.com/res.php",
            params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            timeout=30,
        )
        poll.raise_for_status()
        data = poll.json()
        if data.get("status") == 1:
            return data["request"]
        if data.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha error: {data}")
    raise RuntimeError("2captcha timed out")


def solve_on_demand(timeout: int = 120) -> str:
    """The on-demand fallback path (R23): used for seat acquisition/renewal
    and whenever the token reserve is empty/stale at booking time. Honours
    CAPTCHA_PROVIDER (capsolver/2captcha/race) - this is the one place `race`
    is allowed (R30)."""
    if CAPTCHA_PROVIDER == "race":
        outcomes: dict[str, dict] = {"capsolver": {}, "2captcha": {}}

        def _run(name: str, fn):
            try:
                outcomes[name]["token"] = fn(timeout)
            except Exception as e:
                outcomes[name]["error"] = e

        threads = [
            threading.Thread(target=_run, args=("capsolver", solve_capsolver), daemon=True),
            threading.Thread(target=_run, args=("2captcha", solve_2captcha), daemon=True),
        ]
        for t in threads:
            t.start()
        start = time.time()
        while time.time() - start < timeout:
            for outcome in outcomes.values():
                if outcome.get("token"):
                    return outcome["token"]
            time.sleep(0.25)
        errors = [f"{n}: {o['error']}" for n, o in outcomes.items() if o.get("error")]
        raise RuntimeError(f"race: both providers failed/timed out: {'; '.join(errors)}")
    if CAPTCHA_PROVIDER == "2captcha":
        return solve_2captcha(timeout)
    return solve_capsolver(timeout)


# ---------------------------------------------------------------------------
# CapSolver balance monitoring (R32)
# ---------------------------------------------------------------------------

def check_capsolver_balance() -> None:
    if not CAPSOLVER_API_KEY:
        return
    try:
        resp = requests.post(
            "https://api.capsolver.com/getBalance",
            json={"clientKey": CAPSOLVER_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        balance = data.get("balance")
        if balance is None:
            log_line("INFO", f"CapSolver balance check returned no balance field: {data}")
            return
        log_line("INFO", f"CapSolver balance: ${balance:.2f}")
        if balance < CAPSOLVER_LOW_BALANCE_USD:
            log_line("BALANCE_LOW", f"CapSolver balance ${balance:.2f} below threshold ${CAPSOLVER_LOW_BALANCE_USD:.2f}")
            send_telegram(
                f"⚠️ CapSolver balance is ${balance:.2f}, below the "
                f"${CAPSOLVER_LOW_BALANCE_USD:.2f} alert threshold. The booker "
                f"daemon for {EMBASSY_NAME} will stop being able to solve CAPTCHAs "
                f"once this runs out - top up soon.{RUNTIME_SIGNATURE}"
            )
    except Exception as e:
        log_line("INFO", f"CapSolver balance check failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Date preference (R20-R22) - unchanged rules, reused verbatim from
# estonia_d_visa_booker.py's choose_date()/choose_time(), but exposed here as
# a full ranking (not just a single pick) so the polling loop can implement
# R22's "try every other available date before giving up" within one cycle.
# ---------------------------------------------------------------------------

from datetime import timedelta


def rank_dates(available_dates: list[str]) -> list[str]:
    """Returns available_dates ordered by booking preference:
    1. Dates >= 3 days from today (R20), latest first.
    2. Dates < 3 days from today, earliest first (R20's fallback).
    Querying/booking in this order means the daemon naturally tries the
    preferred date first, then falls back across every other date in
    preference order before giving up (R22), all within a single poll cycle.
    """
    today = datetime.now(timezone.utc).date()
    min_date = today + timedelta(days=3)

    parsed = []
    for d in available_dates:
        try:
            parsed.append((datetime.strptime(d, "%Y-%m-%d").date(), d))
        except ValueError:
            continue

    good = sorted([(dt, s) for dt, s in parsed if dt >= min_date], key=lambda x: x[0], reverse=True)
    fallback = sorted([(dt, s) for dt, s in parsed if dt < min_date], key=lambda x: x[0])
    return [s for _, s in good] + [s for _, s in fallback]


def choose_time(time_labels: list[str]) -> Optional[str]:
    """Prefer earliest slot in 12:00-14:00 (R21); otherwise earliest overall."""
    parsed = []
    for label in time_labels:
        try:
            h, m = (int(x) for x in label.strip().split(":"))
            parsed.append((h, m, label))
        except Exception:
            continue
    if not parsed:
        return None
    midday = sorted([(h, m, lab) for h, m, lab in parsed if 12 <= h < 14])
    if midday:
        return midday[0][2]
    return sorted(parsed)[0][2]


# ---------------------------------------------------------------------------
# Token reserve (R27-R30) - runs only while a seat is live (R28)
# ---------------------------------------------------------------------------

class TokenReserve:
    """Keeps one freshly-solved CAPTCHA token available while a seat is live.
    CapSolver only (R30) - see the module-level comment above for why `race`
    is deliberately excluded here."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_ts: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._token = None
            self._token_ts = 0.0

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                token = solve_capsolver()
                with self._lock:
                    self._token = token
                    self._token_ts = time.time()
                log_line("TOKEN_REFRESHED", "reserve token refreshed")
            except RateLimitExceeded:
                pass  # already alerted by the rate limiter itself
            except Exception as e:
                log_line("INFO", f"Token reserve refresh failed (will retry): {e}")
            # Wait TOKEN_REFRESH_SECONDS, but wake immediately on stop().
            self._stop.wait(TOKEN_REFRESH_SECONDS)

    def take(self) -> Optional[str]:
        """Returns a still-fresh token and clears it (single-use), or None if
        empty/stale (R29). Caller (booking path, R23) must solve on demand
        if this returns None."""
        with self._lock:
            if self._token is None:
                return None
            age = time.time() - self._token_ts
            if age > TOKEN_MAX_AGE_SECONDS:
                log_line("TOKEN_STALE", f"reserve token discarded, age={age:.0f}s > {TOKEN_MAX_AGE_SECONDS}s")
                self._token = None
                return None
            token = self._token
            self._token = None
            return token


# ---------------------------------------------------------------------------
# Seat manager: acquisition, TTL tracking, overlapping renewal (R9-R14)
# ---------------------------------------------------------------------------

class SeatManager:
    def __init__(self):
        self.seat: Optional[Seat] = None
        self.token_reserve = TokenReserve()
        self._renewal_lock = threading.Lock()
        self._renewal_in_progress = False  # R14: cap concurrency
        # Round-2 follow-up Fix 1/2/5 scheduling state. All reset whenever
        # `self.seat` changes (acquire/renewal-swap/drop) so a fresh seat
        # always gets an immediate first slot-poll (matching the spec's
        # timeline in §4.3: "if a slot is already there, book it now") and a
        # fresh date-refresh clock.
        self.last_slot_poll_ts: float = 0.0        # 0.0 => next main-loop tick polls immediately
        self.last_date_refresh_ts: float = 0.0
        self.dates_at_acquisition: list[str] = []  # round-2 Fix 5 baseline - retained for logging, no longer used for suspect detection (round 3 removed that; see Fix 7)

        # Round-3 follow-up state (Fixes 7/8/9/10).
        self.last_forced_refresh_ts: float = 0.0   # Fix 8: debounces forced date-list refreshes
        self.empty_poll_streak: int = 0            # Fix 7: consecutive poll cycles with zero slots on every date
        self.last_trigger_ts: float = 0.0          # Fix 7: for the "is a trigger still fresh" check
        # Fix 10: deliberately NOT reset by drop_seat()/acquire()/renewal -
        # this cooldown is a daemon-wide backstop on reacquisition frequency,
        # not a per-seat property, so it must survive seat swaps.
        self.last_reacquire_ts: float = 0.0

    def has_live_seat(self) -> bool:
        return self.seat is not None and self.seat.alive

    def acquire(self, elapsed_since_trigger: Optional[float] = None) -> bool:
        """R9: acquire a seat - check the F8 gate first, then solve + walk
        steps 0-4. Returns True on success. Never raises; failures are logged
        and Telegrammed per the failure-mode table (spec §8)."""
        log_line("SEAT_ACQUIRING", f"embassy={EMBASSY_ID}/{EMBASSY_NAME}", elapsed_since_trigger)
        try:
            session = http_flow.new_session()
            has_dvisa, services, _ = check_service_offered(session, EMBASSY_ID)
            if not has_dvisa:
                log_line("INFO", f"D-visa not currently offered (F8) - cannot acquire a seat. Services: {services}")
                return False

            captcha_token = solve_on_demand()
            applicant = load_applicant_data()
            seat = establish_seat(EMBASSY_ID, applicant, captcha_token, session=session)
            cache_applicant_on_seat(seat, applicant)
            self.seat = seat
            # Fix 1/2/5 scheduling reset (see __init__'s comment): force an
            # immediate first slot poll, start the date-refresh clock now
            # (the dates we just got via establish_seat() ARE the first
            # refresh), and record the acquisition-time date list as the
            # Fix-5 comparison baseline.
            self.last_slot_poll_ts = 0.0
            self.last_date_refresh_ts = time.time()
            self.last_forced_refresh_ts = time.time()  # Fix 8: the establish_seat() call above IS the first refresh
            self.dates_at_acquisition = list(seat.available_dates)
            self.empty_poll_streak = 0  # Fix 7: fresh seat, fresh evidence window
            self.token_reserve.start()
            log_line(
                "SEAT_ACQUIRED",
                f"dates_seen={seat.available_dates} age=0s",
                elapsed_since_trigger,
            )
            send_telegram(
                f"🎫 Seat acquired for D-visa at {EMBASSY_NAME}. "
                f"Dates currently on the calendar: {seat.available_dates or '(none yet)'}\n"
                f"Now parked - polling free-times every {SLOT_POLL_INTERVAL_SECONDS:.0f}s, "
                f"refreshing the date list every {DATE_REFRESH_INTERVAL_SECONDS:.0f}s "
                f"(immediately, instead, whenever a new trigger arrives).{RUNTIME_SIGNATURE}"
            )
            return True
        except RateLimitExceeded:
            return False
        except Exception as e:
            log_line("INFO", f"Seat acquisition failed: {e}")
            traceback.print_exc()
            return False

    def maybe_renew(self) -> None:
        """R10/R11: start an overlapping renewal once the current seat passes
        SEAT_RENEW_AFTER_SECONDS. R12: only renew if D-visa is still offered
        - otherwise let the old seat expire and go back to waiting for a
        trigger (checked inside begin_overlapping_reacquire). R14: at most
        one renewal/reacquisition in flight at a time, shared with round-3's
        suspect-driven reacquisition (Fix 9) - see begin_overlapping_reacquire()."""
        if not self.has_live_seat():
            return
        if self.seat.age_seconds() < SEAT_RENEW_AFTER_SECONDS:
            return
        self.begin_overlapping_reacquire(reason="ttl_renewal")

    def begin_overlapping_reacquire(self, reason: str) -> bool:
        """Round-3 Fix 9: the SAME overlapping-renewal machinery R11 already
        established for TTL-based renewal, generalised so suspect-driven
        reacquisition (Fix 7) uses it too instead of a separate
        drop-then-acquire path. This is deliberate: round 2's Fix 5 called
        `drop_seat()` THEN `acquire()` synchronously on the main loop for
        suspect reacquisition, which (a) left zero seat coverage for the
        ~15-25s CAPTCHA solve and (b) blocked the main loop's 0.5s
        trigger-detection cadence (R7) for that whole window. Routing
        through this shared path instead means: the existing seat keeps
        serving polls throughout, the replacement is verified (establish_seat()
        raises if it doesn't reach the calendar) before anything is swapped,
        and the whole acquisition happens in a background thread so the main
        loop is never blocked by it.

        Returns True if a reacquisition was actually started (False if one
        was already in flight - R14's concurrency cap - or there was no live
        seat to renew from in the first place).
        """
        if not self.has_live_seat():
            return False
        with self._renewal_lock:
            if self._renewal_in_progress:
                return False
            self._renewal_in_progress = True

        def _do_reacquire():
            try:
                log_line(
                    "SEAT_RENEWING",
                    f"reason={reason} old_seat_age={self.seat.age_seconds():.0f}s" if self.seat else f"reason={reason}",
                )
                session = http_flow.new_session()
                has_dvisa, services, _ = check_service_offered(session, EMBASSY_ID)
                if not has_dvisa:
                    log_line(
                        "INFO",
                        f"R12: D-visa no longer offered - skipping {reason} reacquire, keeping current seat. Services: {services}",
                    )
                    return
                captcha_token = solve_on_demand()
                applicant = load_applicant_data()
                new_seat = establish_seat(EMBASSY_ID, applicant, captcha_token, session=session)
                cache_applicant_on_seat(new_seat, applicant)
                old_age = self.seat.age_seconds() if self.seat else None
                self.seat = new_seat  # atomic reference swap - old seat served every poll up to this instant
                # Same scheduling reset as a fresh acquire() (see __init__'s
                # comment) - a swapped-in seat is, for polling/refresh/
                # suspect-evidence purposes, a brand new seat.
                self.last_slot_poll_ts = 0.0
                self.last_date_refresh_ts = time.time()
                self.last_forced_refresh_ts = time.time()
                self.dates_at_acquisition = list(new_seat.available_dates)
                self.empty_poll_streak = 0
                log_line(
                    "SEAT_SWAPPED",
                    f"reason={reason} old_seat_age={old_age:.0f}s new_dates={new_seat.available_dates}",
                )
            except Exception as e:
                log_line("INFO", f"Seat reacquire ({reason}) failed (old seat kept until it expires/dies): {e}")
            finally:
                with self._renewal_lock:
                    self._renewal_in_progress = False

        threading.Thread(target=_do_reacquire, daemon=True).start()
        return True

    def check_ttl_expiry(self) -> None:
        """R13/R10: a seat older than SEAT_TTL_SECONDS (the measured hard
        ceiling, F5) is treated as expired even without a positive death
        signature yet - this is a sanity backstop, not the primary detection
        mechanism (which is is_seat_dead(), checked on every poll)."""
        if self.seat and self.seat.age_seconds() > SEAT_TTL_SECONDS:
            log_line("SEAT_EXPIRED", f"age={self.seat.age_seconds():.0f}s > SEAT_TTL_SECONDS={SEAT_TTL_SECONDS}")
            self.drop_seat()

    def drop_seat(self) -> None:
        self.seat = None
        self.token_reserve.stop()
        # Reset scheduling state too, so a subsequent acquire() (which also
        # resets these itself) can't be confused by stale timers if anything
        # ever reads them in the gap between drop and re-acquire.
        self.last_slot_poll_ts = 0.0
        self.last_date_refresh_ts = 0.0
        self.last_forced_refresh_ts = 0.0
        self.dates_at_acquisition = []
        self.empty_poll_streak = 0
        # NOTE: last_reacquire_ts is deliberately NOT reset here - see
        # __init__'s comment; it is a daemon-wide cooldown, not a per-seat one.


# ---------------------------------------------------------------------------
# Trigger file watcher (R5-R8)
# ---------------------------------------------------------------------------

class TriggerWatcher:
    def __init__(self):
        self.last_seen_ts: float = 0.0

    def poll_once(self) -> Optional[dict]:
        """Returns the trigger payload if it's newer than the last one we
        saw, else None. R7: poll every 0.5s (handled by the caller's loop),
        treat a newer timestamp as fresh."""
        try:
            if not TRIGGER_FILE.exists():
                return None
            with open(TRIGGER_FILE) as f:
                payload = json.load(f)
            ts = payload.get("ts", 0)
            if ts > self.last_seen_ts:
                self.last_seen_ts = ts
                return payload
        except Exception as e:
            log_line("INFO", f"Trigger file read failed (non-fatal, will retry): {e}")
        return None


def self_detect_once() -> Optional[dict]:
    """R8: opt-in independent detection (default off). Does its own
    check_service_offered() call - this TRIPLES request volume against a
    government portal if left on continuously (once for the monitor's cron,
    once for this), which is exactly why it defaults off and is only meant
    as a debugging/backstop aid, not routine operation."""
    try:
        session = http_flow.new_session()
        has_dvisa, services, _ = check_service_offered(session, EMBASSY_ID)
        if has_dvisa:
            return {"ts": time.time(), "services": services}
    except Exception as e:
        log_line("INFO", f"DAEMON_SELF_DETECT check failed (non-fatal): {e}")
    return None


# ---------------------------------------------------------------------------
# Booking attempt (R19-R26)
# ---------------------------------------------------------------------------

def attempt_booking(seat_mgr: SeatManager, date_iso: str, slot: Slot, elapsed_since_trigger: Optional[float]) -> bool:
    """Returns True if the daemon should stop all further booking attempts
    (i.e. a confirmed booking happened). R25: check the booked flag right
    before every submit, not just at startup - a race against the old
    Playwright booker or a human is possible and this is the last line of
    defence against double-booking."""
    if is_already_booked():
        log_line("INFO", "Already booked (flag set) - skipping submit despite a found slot")
        return True

    log_line(
        "BOOKING_ATTEMPT",
        f"date={date_iso} time={slot.time} slot_id={slot.value} dry_run={DAEMON_DRY_RUN}",
        elapsed_since_trigger,
    )

    seat = seat_mgr.seat
    if seat is None or not seat.alive:
        log_line("BOOKING_FAILED", "no live seat at submit time", elapsed_since_trigger)
        return False

    # R23: use the reserve; if empty/stale, solve on demand and log loudly.
    captcha_token = seat_mgr.token_reserve.take()
    if captcha_token is None:
        log_line("INFO", "Token reserve empty/stale at booking time - solving on demand (this costs the full ~15-25s)")
        try:
            captcha_token = solve_on_demand()
        except Exception as e:
            log_line("BOOKING_FAILED", f"on-demand CAPTCHA solve failed: {e}", elapsed_since_trigger)
            send_telegram(
                f"❌ Slot found for {EMBASSY_NAME} on {date_iso} {slot.time} but the booking "
                f"CAPTCHA could not be solved: {e}\nCheck the portal manually.{RUNTIME_SIGNATURE}"
            )
            return False

    if DAEMON_DRY_RUN:
        # R26: run everything up to but not including the real submit.
        log_line(
            "BOOKING_ATTEMPT",
            f"DAEMON_DRY_RUN=1 - stopping before submit. Would have booked date={date_iso} time={slot.time} slot_id={slot.value}",
            elapsed_since_trigger,
        )
        send_telegram(
            f"🧪 DRY RUN: would have booked D-visa at {EMBASSY_NAME} for {date_iso} {slot.time} "
            f"(slot {slot.value}). No real submit was made.{RUNTIME_SIGNATURE}"
        )
        return False

    try:
        applicant = load_applicant_data()
        confirmed, response_html = submit_booking(
            seat, date_iso, slot.value, captcha_token, notes=applicant.get("specifying_information", "")
        )
    except SeatDeadError:
        log_line("BOOKING_FAILED", "seat died during submit", elapsed_since_trigger)
        seat_mgr.drop_seat()
        return False
    except Exception as e:
        log_line("BOOKING_FAILED", f"submit request failed: {e}", elapsed_since_trigger)
        return False

    if confirmed:
        details = {"date": date_iso, "time": slot.time, "slot_id": slot.value, "embassy": EMBASSY_ID}
        mark_booked(details)
        send_telegram(
            f"✅ BOOKED D-visa appointment for {EMBASSY_NAME}: {date_iso} at {slot.time}. "
            f"~{elapsed_since_trigger:.1f}s from slot detection to submit.{RUNTIME_SIGNATURE}"
        )
        return True
    else:
        # Spec §8: "Booking submitted, confirmation unclear -> Do NOT set the
        # booked flag. Telegram a warning telling the owner to check the
        # portal manually." Never silently swallow an unclear outcome.
        dump_path = dump_html(f"booking_unclear_{EMBASSY_ID}", response_html)
        log_line("BOOKING_FAILED", f"submit response did not confirm success - dump: {dump_path}", elapsed_since_trigger)
        send_telegram(
            f"⚠️ Submitted a booking for {EMBASSY_NAME} ({date_iso} {slot.time}) but could not confirm "
            f"success from the response. CHECK THE PORTAL MANUALLY - it may or may not have gone "
            f"through. (booked flag NOT set){RUNTIME_SIGNATURE}"
        )
        return False


# ---------------------------------------------------------------------------
# R17 date-list refresh scheduling (round-2 Fix 2, round-3 Fix 8)
# ---------------------------------------------------------------------------

def maybe_refresh_dates(seat_mgr: SeatManager, forced: bool, elapsed_since_trigger: Optional[float]) -> bool:
    """Fix 2 (round 2): gate the R17 date-list refresh to
    DATE_REFRESH_INTERVAL_SECONDS instead of doing it on every slot poll - a
    new date appearing is an episode-scale event (minutes apart, F9), not a
    sub-second one. Between refreshes, `seat.available_dates` (the cached
    list) is reused as-is, no request made.

    Fix 8 (round 3): `forced=True` (a new trigger-file update arrived while a
    seat was already live) bypasses the periodic gate above, but is itself
    debounced to FORCED_REFRESH_MIN_INTERVAL_SECONDS - the monitor
    re-announces the SAME episode every ~10s
    (`estonia_visa_monitor_booker_hook.py` writes the trigger file on every
    detection tick, not once per episode), and that is not new information.
    Running a full refresh on every single re-announcement is exactly what
    round 2's Fix 5 got wrong (see IMPLEMENTATION_CHANGELOG.md §9) - this
    function no longer makes any suspect/reacquisition decision at all
    (that logic moved to `check_suspect_and_maybe_reacquire()`, driven by
    actual poll results rather than by what a refresh happens to return -
    see that function's docstring for why round 2's "refreshed dates look
    the same as acquisition time" heuristic was simply wrong).

    Every refresh attempt (forced or not) is recorded in daemon_polls.csv via
    a `__DATE_REFRESH__` pseudo-date row, so refresh behaviour during a real
    episode can be analysed afterward.

    Returns True if the seat was dropped during this call (seat death only,
    as of round 3 - forced refreshes never reacquire directly any more).
    Callers should treat `seat_mgr.seat` as having possibly changed and let
    the next scheduling pass pick up from there.
    """
    seat = seat_mgr.seat
    if seat is None:
        return False

    now = time.time()
    if forced:
        if (now - seat_mgr.last_forced_refresh_ts) < FORCED_REFRESH_MIN_INTERVAL_SECONDS:
            return False  # debounced - the monitor is just re-announcing the same episode
    else:
        if (now - seat_mgr.last_date_refresh_ts) < DATE_REFRESH_INTERVAL_SECONDS:
            return False  # not due yet - reuse the cached date list, no request made

    q0 = time.time()
    try:
        new_dates = resolve_r17_refresh_dates(seat)
    except SeatDeadError:
        log_line("SEAT_DIED", "detected during R17 date-list refresh", elapsed_since_trigger)
        seat_mgr.drop_seat()
        return True
    except Exception as e:
        # Don't let a transient failure make forced/urgent refreshes retry
        # faster than their own interval either - that would defeat Fix 2/8's
        # whole point under exactly the failure conditions (site slowness,
        # 5xx) most likely to cause them.
        seat_mgr.last_date_refresh_ts = now
        if forced:
            seat_mgr.last_forced_refresh_ts = now
        log_line("INFO", f"R17 date-list refresh failed this attempt (using last-known dates): {e}")
        return False

    query_ms = (time.time() - q0) * 1000
    seat_mgr.last_date_refresh_ts = time.time()
    if forced:
        seat_mgr.last_forced_refresh_ts = time.time()
    append_poll_csv(seat.age_seconds(), "__DATE_REFRESH__", len(new_dates), new_dates, query_ms, seat.alive)
    log_line("INFO", f"Date-list refresh (forced={forced}): dates={new_dates} query_ms={query_ms:.0f}")
    return False


# ---------------------------------------------------------------------------
# Suspect-seat detection (round-3 Fixes 7, 9, 10)
# ---------------------------------------------------------------------------

def check_suspect_and_maybe_reacquire(seat_mgr: SeatManager, elapsed_since_trigger: Optional[float]) -> None:
    """Round-3 Fix 7: replaces round 2's WRONG suspect condition entirely.

    Round 2 treated "the refreshed date list is identical to (or a subset
    of) what this seat saw at acquisition time" as suspicious. That carries
    NO signal - real inventory data shows the same single date persisting
    unchanged across an entire ~90-110s episode and even across multiple
    episodes (F9/F10: availability arrives in bursts and does not
    spontaneously churn). Combined with the monitor writing a fresh trigger
    file on every ~10s detection tick for the whole episode (not once per
    episode), round 2's logic declared SUSPECT on the very first
    re-announcement of the SAME still-normal episode, dropped the seat, and
    blocked the main loop on a fresh CAPTCHA solve - repeatedly, for the
    entire episode, without ever actually polling for a slot. See
    IMPLEMENTATION_CHANGELOG.md §9 for the full incident.

    The REAL contradiction worth detecting: the monitor is independently
    telling us D-visa inventory exists (a trigger arrived recently) while
    every date this seat can see keeps coming back with zero bookable slots,
    for long enough that "normal, temporarily sold-out" (R18 - completely
    expected) stops being a plausible explanation on its own. Concretely:

      - `seat_mgr.empty_poll_streak` counts CONSECUTIVE poll_cycle() passes
        in which every ranked date returned zero slots. It resets to 0 the
        instant any date returns a non-empty list (see poll_cycle()).
      - This only counts as suspect if a trigger was seen within
        TRIGGER_FRESH_WINDOW_SECONDS - an empty-poll streak with no recent
        trigger is just... normal quiet monitoring, not a contradiction.
      - The streak must reach SUSPECT_AFTER_EMPTY_POLLS (default 5, ~10s of
        corroborating evidence at the default 2s poll cadence) before firing
        - a single unlucky poll proves nothing.

    Fix 10: even once genuinely suspect, reacquisition is capped at once per
    REACQUIRE_COOLDOWN_SECONDS (default 300s), regardless of cause. This is
    the deliberate backstop: if this heuristic is STILL wrong in some way not
    yet observed, the cost is bounded to one CAPTCHA every 5 minutes, not one
    every 10 seconds. When suppressed by the cooldown, this logs
    REACQUIRE_SUPPRESSED and resets the streak to 0 so the same evidence
    doesn't re-log every single poll cycle for the rest of the cooldown
    window - it has to reaccumulate SUSPECT_AFTER_EMPTY_POLLS worth of fresh
    evidence before being reported again.

    Fix 9: when not suppressed, reacquisition goes through
    `SeatManager.begin_overlapping_reacquire()` - the SAME background,
    verify-then-swap path R11 uses for TTL renewal - never a synchronous
    drop-then-acquire. The current seat keeps serving polls throughout.
    """
    now = time.time()
    trigger_is_fresh = (seat_mgr.last_trigger_ts > 0) and (now - seat_mgr.last_trigger_ts) <= TRIGGER_FRESH_WINDOW_SECONDS
    if not trigger_is_fresh:
        return  # no fresh "inventory exists" signal to contradict - nothing to check
    if seat_mgr.empty_poll_streak < SUSPECT_AFTER_EMPTY_POLLS:
        return

    seat = seat_mgr.seat
    dates = list(seat.available_dates) if seat else []
    trigger_age = now - seat_mgr.last_trigger_ts

    if (now - seat_mgr.last_reacquire_ts) < REACQUIRE_COOLDOWN_SECONDS:
        remaining = REACQUIRE_COOLDOWN_SECONDS - (now - seat_mgr.last_reacquire_ts)
        log_line(
            "REACQUIRE_SUPPRESSED",
            f"suspect condition met (empty_poll_streak={seat_mgr.empty_poll_streak}, "
            f"trigger_age={trigger_age:.0f}s, dates={dates}) but REACQUIRE_COOLDOWN_SECONDS "
            f"not yet elapsed - {remaining:.0f}s remaining",
            elapsed_since_trigger,
        )
        seat_mgr.empty_poll_streak = 0  # must reaccumulate before this logs again
        return

    log_line(
        "DATE_REFRESH_SUSPECT",
        f"empty_poll_streak={seat_mgr.empty_poll_streak} dates={dates} trigger_age={trigger_age:.0f}s - "
        f"monitor reports inventory available but every poll on every date has been empty. "
        f"Starting an overlapping reacquisition.",
        elapsed_since_trigger,
    )
    send_telegram(
        f"⚠️ {EMBASSY_NAME}: the monitor reported D-visa available {trigger_age:.0f}s ago, but the parked "
        f"seat has returned zero slots on {dates or '(no dates)'} for {seat_mgr.empty_poll_streak} "
        f"consecutive polls. Starting a background (overlapping) reacquisition - the current seat "
        f"keeps polling until the replacement is ready.{RUNTIME_SIGNATURE}"
    )
    seat_mgr.last_reacquire_ts = now
    seat_mgr.empty_poll_streak = 0
    seat_mgr.begin_overlapping_reacquire(reason="suspect")


# ---------------------------------------------------------------------------
# Main poll cycle while a seat is live (R15-R22, R35)
# ---------------------------------------------------------------------------

def poll_cycle(seat_mgr: SeatManager, elapsed_since_trigger: Optional[float]) -> bool:
    """One full pass over every available date, sequentially (R16). Returns
    True if a confirmed booking happened (caller should stop the whole
    daemon's booking activity for this embassy). Any SeatDeadError anywhere
    in the cycle drops the seat and returns False so the outer loop goes
    back to waiting for a trigger / re-acquisition.

    Called by the main loop only when SLOT_POLL_INTERVAL_SECONDS has elapsed
    (round-2 follow-up Fix 1) - see main()'s scheduling around
    `seat_mgr.last_slot_poll_ts`.
    """
    seat = seat_mgr.seat
    if seat is None:
        return False

    # R17/Fix 2: refresh the date list on its own schedule (not every poll -
    # see maybe_refresh_dates()'s docstring). As of round 3 this can only
    # drop the seat on an actual SeatDeadError - it no longer makes any
    # suspect/reacquisition decision (see check_suspect_and_maybe_reacquire()
    # below for that, now driven by poll results instead).
    if maybe_refresh_dates(seat_mgr, forced=False, elapsed_since_trigger=elapsed_since_trigger):
        return False
    seat = seat_mgr.seat
    if seat is None:
        return False

    if is_already_booked():
        return True  # R25: stop polling for booking purposes once booked

    any_slots_this_cycle = False
    for date_iso in rank_dates(seat.available_dates):
        q0 = time.time()
        try:
            slots = query_free_times(seat, date_iso)
        except SeatDeadError:
            log_line("SEAT_DIED", f"detected while querying {date_iso}", elapsed_since_trigger)
            seat_mgr.drop_seat()
            return False
        except Exception as e:
            log_line("INFO", f"Query failed for {date_iso} (non-fatal, will retry next cycle): {e}")
            continue
        query_ms = (time.time() - q0) * 1000
        times = [s.time for s in slots]
        append_poll_csv(seat.age_seconds(), date_iso, len(slots), times, query_ms, seat.alive)
        log_line("POLL_RESULT", f"date={date_iso} slots={len(slots)} times={times} query_ms={query_ms:.0f}")

        if not slots:
            continue  # R18: normal, keep going

        # Fix 7: any non-empty result anywhere in this cycle immediately
        # resets the empty-poll streak - the seat is demonstrably able to
        # see real inventory, which is the opposite of suspect.
        any_slots_this_cycle = True
        seat_mgr.empty_poll_streak = 0

        log_line("SLOT_FOUND", f"date={date_iso} times={times}", elapsed_since_trigger)
        selected_time = choose_time(times)
        chosen_slot = next((s for s in slots if s.time == selected_time), slots[0])
        booked = attempt_booking(seat_mgr, date_iso, chosen_slot, elapsed_since_trigger)
        if booked:
            return True
        # R22: preferred date failed to yield a confirmed booking (already
        # taken by the time of submit, etc.) - try every other date in this
        # same cycle (the for-loop naturally continues) instead of giving up.

    if not any_slots_this_cycle:
        # Fix 7: every ranked date came back empty this cycle - one more
        # data point toward (or away from) "suspect". A single empty cycle
        # proves nothing (R18); check_suspect_and_maybe_reacquire() only
        # acts once this has repeated SUSPECT_AFTER_EMPTY_POLLS times AND a
        # trigger is still fresh.
        seat_mgr.empty_poll_streak += 1
    check_suspect_and_maybe_reacquire(seat_mgr, elapsed_since_trigger)

    return False


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

_shutdown_requested = threading.Event()


def _handle_sigterm(signum, frame) -> None:
    log_line("INFO", f"Received signal {signum} - finishing any in-flight work then shutting down (R40)")
    _shutdown_requested.set()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    if not acquire_daemon_lock():
        log_line("INFO", "Another daemon instance already holds the lock - exiting (R41)")
        return 1

    log_line(
        "INFO",
        f"Daemon starting. embassy={EMBASSY_ID}/{EMBASSY_NAME} dry_run={DAEMON_DRY_RUN} "
        f"self_detect={DAEMON_SELF_DETECT} captcha_provider={CAPTCHA_PROVIDER}",
    )
    send_telegram(
        f"🚀 Estonia D-visa booker daemon started for {EMBASSY_NAME}"
        f"{' (DRY RUN)' if DAEMON_DRY_RUN else ''}.{RUNTIME_SIGNATURE}"
    )

    seat_mgr = SeatManager()
    trigger_watcher = TriggerWatcher()

    trigger_ts: Optional[float] = None  # for elapsed-since-trigger logging
    last_self_detect = 0.0
    last_balance_check = 0.0
    consecutive_errors = 0

    try:
        while not _shutdown_requested.is_set():
            loop_started = time.time()
            try:
                # --- Trigger detection (R7) ---
                trigger = trigger_watcher.poll_once()
                if DAEMON_SELF_DETECT and (time.time() - last_self_detect) >= SELF_DETECT_INTERVAL_SECONDS:
                    last_self_detect = time.time()
                    self_trigger = self_detect_once()
                    if self_trigger and (trigger is None or self_trigger["ts"] > trigger_watcher.last_seen_ts):
                        trigger = self_trigger
                        trigger_watcher.last_seen_ts = self_trigger["ts"]

                if trigger is not None:
                    trigger_ts = trigger["ts"]
                    elapsed = time.time() - trigger_ts
                    log_line("TRIGGER_RECEIVED", f"services={trigger.get('services')}", elapsed)
                    # Fix 7: record that a trigger was just seen, regardless
                    # of seat state - this is the "inventory exists right
                    # now, independently confirmed" signal
                    # check_suspect_and_maybe_reacquire() checks freshness
                    # against. Uses the trigger's own timestamp (when the
                    # monitor actually detected availability), not the time
                    # the daemon happened to notice the file.
                    seat_mgr.last_trigger_ts = trigger_ts
                    if not seat_mgr.has_live_seat() and not is_already_booked():
                        seat_mgr.acquire(elapsed_since_trigger=elapsed)
                    elif seat_mgr.has_live_seat() and not is_already_booked():
                        # A NEW trigger arrived while a seat is already live -
                        # opportunistically refresh the date list sooner than
                        # the periodic DATE_REFRESH_INTERVAL_SECONDS cadence,
                        # since the monitor is independently confirming
                        # inventory exists right now. Debounced to
                        # FORCED_REFRESH_MIN_INTERVAL_SECONDS (Fix 8) - the
                        # monitor re-announces the SAME episode every ~10s,
                        # which is not new information each time. This no
                        # longer makes any suspect/reacquisition decision on
                        # its own (round 2's Fix 5 bug) - that is now driven
                        # by actual poll results in
                        # check_suspect_and_maybe_reacquire(), called from
                        # poll_cycle().
                        maybe_refresh_dates(seat_mgr, forced=True, elapsed_since_trigger=elapsed)

                # --- Seat maintenance (R10-R14) ---
                if seat_mgr.has_live_seat():
                    seat_mgr.check_ttl_expiry()
                if seat_mgr.has_live_seat():
                    seat_mgr.maybe_renew()

                # --- Periodic CapSolver balance check (R32) ---
                if (time.time() - last_balance_check) >= BALANCE_CHECK_INTERVAL_SECONDS:
                    last_balance_check = time.time()
                    check_capsolver_balance()

                # --- Poll for slots while a seat is live (R15-R22) ---
                # Round-2 follow-up Fix 1: this used to run on every 0.5s
                # main-loop tick (~2 req/s while parked, ~10x the monitor's
                # own rate). Gated here to SLOT_POLL_INTERVAL_SECONDS so the
                # main loop keeps its 0.5s cadence for trigger detection (R7)
                # while slot polling runs at its own, much slower, cadence.
                if seat_mgr.has_live_seat():
                    now = time.time()
                    if (now - seat_mgr.last_slot_poll_ts) >= SLOT_POLL_INTERVAL_SECONDS:
                        seat_mgr.last_slot_poll_ts = now
                        elapsed = (time.time() - trigger_ts) if trigger_ts else None
                        booked_now = poll_cycle(seat_mgr, elapsed)
                        if booked_now:
                            log_line("INFO", "Booking confirmed - continuing to monitor, but no further booking attempts will be made")

                consecutive_errors = 0

            except Exception as e:
                # Nothing above should normally raise all the way out here
                # (each sub-step guards its own errors), but this is the
                # last line of defence: log, back off exponentially capped
                # at 30s, never spin (spec §8: "Site returns 5xx / times out").
                consecutive_errors += 1
                backoff = min(30, 2 ** consecutive_errors)
                log_line("INFO", f"Unhandled error in main loop (backing off {backoff}s): {e}")
                traceback.print_exc()
                time.sleep(backoff)
                continue

            # Sleep the remainder of the trigger-poll interval (R7's 0.5s
            # cadence), accounting for however long this iteration took.
            elapsed_loop = time.time() - loop_started
            time.sleep(max(0.0, TRIGGER_POLL_INTERVAL_SECONDS - elapsed_loop))

    finally:
        seat_mgr.token_reserve.stop()
        log_line("INFO", "Daemon shutting down cleanly (R40)")
        send_telegram(f"🛑 Estonia D-visa booker daemon stopped for {EMBASSY_NAME}.{RUNTIME_SIGNATURE}")
        release_daemon_lock()

    return 0


if __name__ == "__main__":
    sys.exit(main())
