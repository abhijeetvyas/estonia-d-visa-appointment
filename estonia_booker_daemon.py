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
    find_delete_link,
)

# ---------------------------------------------------------------------------
# Configuration (spec §6.9 - every one env-overridable, defaults as spec'd)
# ---------------------------------------------------------------------------

EMBASSY_ID = os.environ.get("EMBASSY_ID", "40")
EMBASSY_NAME = os.environ.get("EMBASSY_NAME", "New Delhi")

# Round 13 (owner's request, 2026-08-05): run a second, fully independent
# hunt for a different applicant against the same embassy. Without this, a
# second daemon instance for the same EMBASSY_ID would share
# BOOKED_STATE_FILE/DAEMON_LOCK_FILE with the first applicant's instance -
# the lock would refuse to let it start, and should_book_date() would treat
# applicant 1's held appointment as something applicant 2 must "beat" to
# book at all. Setting BOOKING_PROFILE namespaces the state file, lock file,
# log, poll CSV and dump directory by this label, so a second instance is a
# genuinely separate booking hunt: it starts from "nothing booked yet" and
# takes the earliest date it can get, same as applicant 1's instance did
# originally. TRIGGER_FILE is deliberately NOT namespaced - both instances
# must react to the same monitor detection for this embassy. Leave unset for
# the original single-applicant behaviour (identical filenames to before).
BOOKING_PROFILE = os.environ.get("BOOKING_PROFILE", "").strip()
_PROFILE_SUFFIX = f"_{BOOKING_PROFILE}" if BOOKING_PROFILE else ""

SEAT_TTL_SECONDS = int(os.environ.get("SEAT_TTL_SECONDS", "1440"))          # R10: measured ceiling (F5)
SEAT_RENEW_AFTER_SECONDS = int(os.environ.get("SEAT_RENEW_AFTER_SECONDS", "1200"))  # R10: operating number

# Round-5 Fix 16: the real predictor of seat death. Three live runs at three
# different poll cadences all died at ~95-100 REQUESTS, not at any fixed
# duration - see SeatManager.maybe_renew()'s docstring. 70 leaves ~30 requests
# of margin, which at the 2s poll cadence is ~60s of slack for an overlapping
# replacement to finish establishing before the old seat would have died.
SEAT_RENEW_AFTER_REQUESTS = int(os.environ.get("SEAT_RENEW_AFTER_REQUESTS", "70"))

# Round-5 Fix 17: minimum gap between "the seat died" and starting a
# replacement. Deliberately short (seat death is normal every ~190s, not an
# error) - it exists only to stop a tight spin if acquisition fails instantly.
SEAT_DEATH_REACQUIRE_COOLDOWN_SECONDS = float(
    os.environ.get("SEAT_DEATH_REACQUIRE_COOLDOWN_SECONDS", "15")
)
SLOT_POLL_INTERVAL_SECONDS = float(os.environ.get("SLOT_POLL_INTERVAL_SECONDS", "2"))  # R15
# Round-2 follow-up Fix 2: a new date appearing is an episode-scale event
# (minutes apart, F9), not a sub-second one - refreshing the date list on
# every slot poll (every ~0.5-2s) doubled request volume for no benefit.
# This is gated independently of SLOT_POLL_INTERVAL_SECONDS so the two
# cadences can be tuned separately; see maybe_refresh_dates().
DATE_REFRESH_INTERVAL_SECONDS = float(os.environ.get("DATE_REFRESH_INTERVAL_SECONDS", "10"))

# Round-7: when the calendar shows NO dates at all - i.e. between availability
# episodes, which is most of the time - refreshing every 10s is pure waste,
# and expensive waste: a seat has a hard budget of ~95-100 requests (F5
# corrected), so a 10s idle cadence burns it in ~17 minutes.
#
# That matters far more than it sounds. A seat cannot be ACQUIRED unless
# D-visa is currently offered (F8), but an existing one survives perfectly
# well after an episode ends - observed 2026-08-03 at seat_age=452s with an
# empty date list. So a seat that outlives the gap between episodes is
# already parked when the next one starts, and sees the slot ~1s after the
# trigger instead of ~20s after it (the CAPTCHA-bound acquisition). Episodes
# came 5-40 minutes apart that day; at 60s idle the same budget lasts ~100
# minutes, which covers nearly all of those gaps.
#
# Responsiveness is not sacrificed: a trigger forces an immediate refresh
# (subject only to FORCED_REFRESH_MIN_INTERVAL_SECONDS), so the moment the
# monitor sees D-visa, the parked seat re-reads the calendar within ~1s.
IDLE_DATE_REFRESH_INTERVAL_SECONDS = float(os.environ.get("IDLE_DATE_REFRESH_INTERVAL_SECONDS", "60"))
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
# Round 11: this became dead config in round 8 (the periodic refresh loop was
# replaced by a deficit-driven pool) and now has a real job again: the age at
# which a REPLACEMENT solve starts, while the existing token stays usable.
#
# The distinction matters more than the exact numbers. Replacement is not
# instant - it takes 15-25s - so a purely reactive pool (prune at
# TOKEN_MAX_AGE_SECONDS, then solve) is short for that entire window, and if
# both tokens age out together it is empty. Starting the replacement at 75s
# means the new token lands around 90-100s while the old one is still valid,
# so the pool never dips. Raising the max age instead would have made this
# worse, not better: the token would be closer to expiry when replacement
# began.
TOKEN_REFRESH_SECONDS = int(os.environ.get("TOKEN_REFRESH_SECONDS", "75"))   # R27
# Round 12: raised 100 -> 110. This is the age at which a token stops being
# handed out at all, NOT the age at which a replacement starts (that is
# TOKEN_REFRESH_SECONDS=75). Raising it therefore costs nothing - the
# replacement cadence, and so the solve rate, is unchanged - and widens the
# overlap covering a slow solve from 25s to 35s.
#
# The narrower expiry margin (10s against ~120s validity, instead of 20s) only
# ever applies to a token that is being used as a LAST RESORT: with proactive
# replacement running, anything older than 100s is only reached if the pool
# has nothing fresher, i.e. solves are failing or a burst consumed everything.
# In that situation the alternative is not a safer token, it is no token at
# all - and if the old one has expired the submit simply fails and the poll
# loop retries 2s later with a fresh one. A maybe-valid token strictly beats
# nothing when the fallback is recoverable.
TOKEN_MAX_AGE_SECONDS = int(os.environ.get("TOKEN_MAX_AGE_SECONDS", "110"))  # R29
# Round 8: raised 60 -> 250. A continuously-maintained pool of 2 tokens with
# ~120s validity costs roughly 60 solves/hour on its own, so the old cap would
# have throttled the reserve permanently and silently - the guard would have
# become the bug. 250 still bounds a runaway loop (~$0.20/hour) while leaving
# the intended steady state well clear of the ceiling.
MAX_SOLVES_PER_HOUR = int(os.environ.get("MAX_SOLVES_PER_HOUR", "250"))     # R31

# Round 8: how many pre-solved tokens to keep in hand at all times. 2 is the
# minimum that actually helps - see TokenReserve's docstring. One covers seat
# acquisition; the second covers the submit that follows ~2s later. With only
# one, the submit still waits 15-25s and the slot is gone.
TOKEN_RESERVE_SIZE = int(os.environ.get("TOKEN_RESERVE_SIZE", "2"))

# How often the maintenance loop checks whether the pool is short. Cheap: a
# no-op unless a token was consumed or aged out, so this governs how quickly
# a gap is noticed, not how often anything is spent.
TOKEN_POOL_CHECK_INTERVAL_SECONDS = float(os.environ.get("TOKEN_POOL_CHECK_INTERVAL_SECONDS", "5"))
# Round 9 (owner's request): alert below $1, and REPEAT hourly while it stays
# low. A single alert is easy to miss, and an empty balance silently reverts
# the daemon to solving on demand at every step - slower than before the pool
# existed - with no further warning.
CAPSOLVER_LOW_BALANCE_USD = float(os.environ.get("CAPSOLVER_LOW_BALANCE_USD", "1.00"))  # R32
LOW_BALANCE_REALERT_SECONDS = float(os.environ.get("LOW_BALANCE_REALERT_SECONDS", "3600"))

# Round 9 (owner's request): Telegram the available time slots as they are
# seen. Sent on CHANGE, not on every poll - SLOT_FOUND fires every 2s while a
# seat is parked (80+ times in a 3-minute test run), so an unconditional send
# would be pure spam and would bury the messages that matter. A minimum
# interval guards against a slot list that flaps between two states.
SLOTS_NOTIFY_MIN_INTERVAL_SECONDS = float(os.environ.get("SLOTS_NOTIFY_MIN_INTERVAL_SECONDS", "30"))

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
# (estonia_d_visa_booker.py's STATE_FILE convention) when BOOKING_PROFILE is
# unset - R25 requires never booking twice, and that guarantee only holds if
# both bookers that could ever run against this embassy (old Playwright
# fallback, new daemon) check and set the same flag rather than maintaining
# independent, unsynced ideas of whether a booking already exists. Note the
# old Playwright booker does NOT understand BOOKING_PROFILE - it always reads
# the unsuffixed file, so it only ever sees/protects applicant 1's booking.
BOOKED_STATE_FILE = STATE_DIR / f"booker_state_{EMBASSY_ID}{_PROFILE_SUFFIX}.json"
DAEMON_LOCK_FILE = Path(f"/tmp/estonia_booker_daemon_{EMBASSY_ID}{_PROFILE_SUFFIX}.lock")  # R41

DAEMON_LOG_FILE = SCRIPT_DIR / f"daemon{_PROFILE_SUFFIX}.log"          # R33
POLLS_CSV_FILE = SCRIPT_DIR / f"daemon_polls{_PROFILE_SUFFIX}.csv"     # R35
DUMPS_DIR = SCRIPT_DIR / f"daemon_dumps{_PROFILE_SUFFIX}"              # R36

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


def append_poll_csv(seat_age_s: float, date: str, slot_count: int, slot_times: list, query_ms: float,
                    seat_alive: bool, requests_used: int = -1) -> None:
    """R35: utc, seat_age_s, date, slot_count, slot_times, query_ms, seat_alive.

    Round-5 Fix 16 adds requests_used as a trailing column. The observed
    ~95-100-request ceiling comes from only three runs on one embassy, so
    every poll now records the counter - that turns the ceiling into
    something measurable across many runs rather than something inferred
    once and then trusted forever, which is exactly the mistake the original
    "1440 seconds" figure represented.
    """
    global _csv_header_written
    try:
        with open(POLLS_CSV_FILE, "a") as f:
            if not _csv_header_written:
                f.write("utc,seat_age_s,date,slot_count,slot_times,query_ms,seat_alive,requests_used\n")
                _csv_header_written = True
            utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            times = "|".join(slot_times)
            f.write(
                f"{utc},{seat_age_s:.1f},{date},{slot_count},{times},"
                f"{query_ms:.0f},{seat_alive},{requests_used}\n"
            )
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
    if BOOKING_PROFILE:
        message = f"[{BOOKING_PROFILE}] {message}"
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


def currently_booked_date() -> Optional[str]:
    """The date (YYYY-MM-DD) of the best appointment currently held, or None.

    Falls back to the legacy `booked: true` flag with no recorded date - a
    state file written by the old Playwright booker before this change. In
    that case we know a booking exists but not for when, so `should_book_date`
    treats it conservatively.
    """
    state = load_booked_state()
    d = state.get("booked_date")
    if d:
        return d
    if state.get("booked"):
        return state.get("booking_details", {}).get("date")
    return None


def should_book_date(candidate_iso: str) -> bool:
    """Round-6: replaces the old permanent `is_already_booked()` gate.

    The original design stopped booking forever once one appointment was
    confirmed. The owner wants the opposite: keep hunting indefinitely, and
    whenever a STRICTLY EARLIER date becomes bookable, take it - he deletes
    the superseded appointment himself. So the gate is no longer "have we
    booked?" but "would this be an improvement?".

    Returns True if nothing is booked yet, or if `candidate_iso` is earlier
    than what we already hold. Note `rank_dates()` has already excluded today
    and past dates, so anything reaching here is a real future date.
    """
    held = currently_booked_date()
    if not held:
        return True
    try:
        return (
            datetime.strptime(candidate_iso, "%Y-%m-%d").date()
            < datetime.strptime(held, "%Y-%m-%d").date()
        )
    except ValueError:
        # Unparseable stored date: refuse to rebook rather than risk churning
        # bookings against a state file we cannot reason about. Visible in the
        # log so it gets fixed rather than silently suppressing all rebooking.
        log_line("INFO", f"Stored booked_date {held!r} is unparseable - not rebooking until it is fixed")
        return False


def mark_booked(details: dict) -> Optional[str]:
    """Record a confirmed booking and return the date it superseded (if any),
    so the caller can tell the owner which appointment to go and delete.

    Keeps the legacy `booked: true` key so the old Playwright booker (which
    shares this state file) still sees a booking exists, and appends to
    `booking_history` so the full chain of improvements is auditable rather
    than being overwritten each time.
    """
    state = load_booked_state()
    previous = state.get("booked_date") or state.get("booking_details", {}).get("date")

    state["booked"] = True  # legacy key - the old booker still reads this
    state["booked_date"] = details.get("date")
    state["booked_time"] = details.get("time")
    state["booked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["booking_details"] = details
    state["booked_by"] = "estonia_booker_daemon"
    history = state.get("booking_history", [])
    history.append({
        "at": state["booked_at"],
        "date": details.get("date"),
        "time": details.get("time"),
        "superseded": previous,
    })
    state["booking_history"] = history
    save_booked_state(state)

    if previous:
        log_line("BOOKING_SUCCESS", f"Booked {details} - SUPERSEDES {previous}, which must be deleted manually")
    else:
        log_line("BOOKING_SUCCESS", f"Booked {details} (first booking). Still hunting for an earlier date.")
    return previous


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

_last_low_balance_alert_ts: float = 0.0

# Round 9: what slot list we last told the owner about, and when.
_last_slots_signature: str = ""
_last_slots_notify_ts: float = 0.0


def notify_available_slots(date_iso: str, times: list, held: Optional[str]) -> None:
    """Telegram the currently-visible time slots for a date.

    Deliberately change-driven. While a seat is parked the poll loop produces
    a SLOT_FOUND every 2 seconds - during the 2026-08-03 T7 run that was 80+
    identical results in three minutes. Sending each one would make the
    channel useless precisely when a real booking message needs to stand out.
    So: send when the (date, times) signature changes, and never more often
    than SLOTS_NOTIFY_MIN_INTERVAL_SECONDS.
    """
    global _last_slots_signature, _last_slots_notify_ts
    signature = f"{date_iso}:{','.join(times)}"
    now = time.time()
    if signature == _last_slots_signature:
        return
    if (now - _last_slots_notify_ts) < SLOTS_NOTIFY_MIN_INTERVAL_SECONDS:
        return
    _last_slots_signature = signature
    _last_slots_notify_ts = now

    if held:
        tail = (
            f"\n\nYou currently hold {held}. These will only be booked if the date is "
            f"EARLIER than that."
        )
    else:
        tail = "\n\nNothing booked yet - the earliest of these is being attempted now."
    try:
        send_telegram(
            f"🕒 Slots visible at {EMBASSY_NAME} — {date_iso}\n\n"
            f"{', '.join(times) if times else '(none)'}{tail}{RUNTIME_SIGNATURE}"
        )
    except Exception as e:
        log_line("INFO", f"Slot-availability Telegram failed (non-fatal): {e}")


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
            global _last_low_balance_alert_ts
            log_line("BALANCE_LOW", f"CapSolver balance ${balance:.2f} below threshold ${CAPSOLVER_LOW_BALANCE_USD:.2f}")
            # Round 9: repeat hourly rather than once. The balance check runs
            # every 15 minutes, so without this gate a low balance would
            # either alert four times an hour or (as before) exactly once and
            # then go quiet while the daemon quietly degrades.
            if (time.time() - _last_low_balance_alert_ts) >= LOW_BALANCE_REALERT_SECONDS:
                _last_low_balance_alert_ts = time.time()
                hours_left = balance / 0.05 if balance > 0 else 0  # ~$0.05/hr at a 2-token pool
                send_telegram(
                    f"⚠️ CapSolver balance is ${balance:.2f} (below ${CAPSOLVER_LOW_BALANCE_USD:.2f}).\n\n"
                    f"At the current pool cost of roughly $0.05/hour that is about "
                    f"{hours_left:.0f} hours of runway.\n\n"
                    f"When it runs out the daemon does not stop - it silently falls back to "
                    f"solving CAPTCHAs on demand, which is SLOWER than before the token pool "
                    f"existed. Top up at capsolver.com.\n\n"
                    f"(Repeats hourly while low.){RUNTIME_SIGNATURE}"
                )
        else:
            # Recovered - reset so the next dip alerts immediately rather
            # than waiting out an old hourly window.
            _last_low_balance_alert_ts = 0.0
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
    """Returns available_dates ordered by booking preference: EARLIEST FIRST,
    excluding today and anything in the past.

    Changed 2026-08-03 on the owner's instruction, replacing the original
    R20/R21 rule ("latest date at least 3 days out, fall back to earliest").
    Two separate reasons, both from him directly:

      * The 3-day minimum was a comfort preference, not a requirement. Any
        date that is not today works - tomorrow included.
      * Earlier is strictly better. The system no longer stops after one
        booking (see should_book_date): it keeps hunting and rebooks whenever
        an EARLIER date appears, and he deletes the later one by hand. A
        latest-first ordering would fight that goal on every cycle.

    Today itself is excluded because a same-day appointment is not usefully
    bookable - and past dates are excluded because the calendar has been
    observed listing them.
    """
    today = datetime.now(timezone.utc).date()

    parsed = []
    for d in available_dates:
        try:
            parsed.append((datetime.strptime(d, "%Y-%m-%d").date(), d))
        except ValueError:
            continue

    usable = sorted([(dt, s) for dt, s in parsed if dt > today], key=lambda x: x[0])
    return [s for _, s in usable]


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
    """A continuously-maintained POOL of pre-solved CAPTCHA tokens.

    ROUND 8 (2026-08-03) - what changed and why
    -------------------------------------------
    Previously this held ONE token and ran only while a seat was live (R28).
    That left the expensive path uncovered. Acquiring a seat needs a token at
    the very first gate, ~1.5s into the flow, so it could never be prewarmed
    in time - measured cost, repeatedly, ~20s from trigger to being parked on
    the calendar. Every New Delhi episode was therefore observed 20 seconds
    late, and on 2026-08-03 six consecutive episodes showed the slot already
    gone by the time we looked.

    Why the pool needs to be >= 2, not 1
    ------------------------------------
    A single warm token makes ACQUISITION instant but leaves the SUBMIT
    stranded: acquire at t=0 consumes the token, the seat is parked by t=2s,
    a slot is found at t=3s, and the submit then waits 15-25s for the next
    solve - so the booking is no faster than before, we just watched it get
    taken. Two tokens covers acquire-then-immediately-submit, which is
    precisely the sequence that wins a slot. TOKEN_RESERVE_SIZE is
    configurable; below 2 the daemon logs a warning at startup.

    Tokens are single-use (Google consumes them at siteverify) and valid for
    ~120s, so the pool is refreshed continuously against TOKEN_MAX_AGE_SECONDS
    rather than on a fixed timer per token.

    CapSolver only (R30): racing providers exists to cut latency variance, and
    nothing waits on a background pool - racing here would double the cost for
    no benefit. `race` remains the on-demand fallback when the pool is dry.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: list[tuple[str, float]] = []   # (token, solved_at), freshest last
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._solves_in_flight = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Round 8: started once at daemon startup and left running for the
        process lifetime, NOT tied to seat state. The whole point is to have
        tokens in hand BEFORE a trigger arrives; a pool that only fills once
        a seat exists cannot help the seat get created."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log_line(
            "INFO",
            f"Token reserve started: target={TOKEN_RESERVE_SIZE} fresh tokens, "
            f"replacement starts at {TOKEN_REFRESH_SECONDS}s, tokens usable until "
            f"{TOKEN_MAX_AGE_SECONDS}s, CapSolver only",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._tokens.clear()

    def prewarm(self) -> None:
        """Kept for compatibility with the round-4 call sites. With a
        continuously-maintained pool this is normally a no-op, but it still
        forces an immediate top-up if the pool happens to be short right when
        a trigger lands."""
        if self._stop.is_set():
            return
        threading.Thread(target=self._top_up, kwargs={"reason": "prewarm"}, daemon=True).start()

    # -- internals ---------------------------------------------------------

    def _prune_locked(self) -> None:
        """Drop tokens too old to be safely usable. Caller holds the lock."""
        now = time.time()
        keep = [(t, ts) for (t, ts) in self._tokens if (now - ts) <= TOKEN_MAX_AGE_SECONDS]
        dropped = len(self._tokens) - len(keep)
        if dropped:
            log_line("TOKEN_STALE", f"{dropped} reserve token(s) expired unused and were discarded")
        self._tokens = keep

    def _fresh_count_locked(self) -> int:
        """Tokens young enough that no replacement is needed yet. Caller holds
        the lock.

        Deliberately counts against TOKEN_REFRESH_SECONDS, not
        TOKEN_MAX_AGE_SECONDS: a token past the refresh age is still perfectly
        usable (take() will hand it out) but no longer counts toward the
        pool's target, which is what makes a replacement start early enough to
        land before the old one expires.
        """
        now = time.time()
        return sum(1 for (_t, ts) in self._tokens if (now - ts) <= TOKEN_REFRESH_SECONDS)

    def _reserve_slots(self) -> int:
        """Atomically work out how many solves are still needed AND claim
        them, returning that count.

        Claiming under the same lock as the calculation is the point.
        Previously `_solves_in_flight` was incremented inside each spawned
        thread, so two callers arriving together (the maintenance loop and a
        post-take top-up, say) could both compute the same deficit before
        either had registered its intent - and each would then spend real
        money solving CAPTCHAs the other had already covered.
        """
        with self._lock:
            self._prune_locked()
            need = max(0, TOKEN_RESERVE_SIZE - self._fresh_count_locked() - self._solves_in_flight)
            self._solves_in_flight += need
            return need

    def _solve_one(self, reason: str) -> None:
        """Caller must already have claimed a slot via `_reserve_slots()`;
        this only releases it."""
        try:
            token = solve_capsolver()
            with self._lock:
                self._tokens.append((token, time.time()))
                depth = len(self._tokens)
            log_line("TOKEN_REFRESHED", f"reserve token added (reason={reason}, pool={depth}/{TOKEN_RESERVE_SIZE})")
        except RateLimitExceeded:
            pass  # the rate limiter has already logged and alerted
        except Exception as e:
            log_line("INFO", f"Token reserve solve failed (reason={reason}, will retry): {e}")
        finally:
            with self._lock:
                self._solves_in_flight -= 1

    def _top_up(self, reason: str) -> None:
        """Solve however many tokens are missing, in parallel. Parallel
        because a serial top-up of an empty pool would take N x 20s, and the
        window we are trying to cover is shorter than that."""
        need = self._reserve_slots()
        if need <= 0:
            return
        threads = [
            threading.Thread(target=self._solve_one, kwargs={"reason": reason}, daemon=True)
            for _ in range(need)
        ]
        for t in threads:
            t.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._top_up(reason="maintain")
            except Exception as e:
                log_line("INFO", f"Token reserve maintenance error (continuing): {e}")
            # Poll frequently but cheaply: _top_up() is a no-op unless the
            # pool is actually short, so this costs nothing when full. The
            # short interval is what lets a token expiring or being consumed
            # be replaced promptly rather than on a slow fixed tick.
            self._stop.wait(TOKEN_POOL_CHECK_INTERVAL_SECONDS)

    # -- public ------------------------------------------------------------

    def take(self) -> Optional[str]:
        """Pop the FRESHEST usable token, or None if the pool is dry.

        Freshest rather than oldest deliberately: the caller is about to use
        it immediately, so maximising remaining validity matters more than
        avoiding waste, and an older token left behind may still serve a
        subsequent take a second later.
        """
        with self._lock:
            self._prune_locked()
            if not self._tokens:
                depth = 0
                result = None
            else:
                token, _ts = self._tokens.pop()   # freshest is last
                depth = len(self._tokens)
                result = token
        if result is not None:
            log_line("INFO", f"Took a pre-solved token from the reserve (pool now {depth}/{TOKEN_RESERVE_SIZE})")
        threading.Thread(target=self._top_up, kwargs={"reason": "post-take"}, daemon=True).start()
        return result

    def depth(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._tokens)


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
        # Fix 17: separate from last_reacquire_ts (which gates the round-3
        # SUSPECT path at 300s); death recovery needs a much shorter gate.
        self.last_death_reacquire_ts: float = 0.0

        # Round-4 follow-up state (Fixes 13/14).
        # Fix 13: at most one booking attempt (on-demand solve + eventual
        # submit) in flight at a time - guards against poll_cycle() finding
        # the same or another slot again before a previous background
        # booking attempt has finished.
        self.booking_lock = threading.Lock()
        self.booking_in_progress: bool = False
        # Fix 13: serializes every HTTP call that touches this seat's live
        # session (free-times queries, R17 date-list refreshes, and the
        # final submit) so a backgrounded on-demand-solve-then-submit can
        # never interleave with a concurrent free-times query on the SAME
        # session (R16's "concurrent queries may corrupt session state" -
        # the CAPTCHA solve itself touches no session state at all and so
        # deliberately runs OUTSIDE this lock; only the brief final submit
        # needs it).
        self.session_lock = threading.Lock()

    def has_live_seat(self) -> bool:
        return self.seat is not None and self.seat.alive

    def acquire(self, elapsed_since_trigger: Optional[float] = None) -> bool:
        """R9: acquire a seat - check the F8 gate first, then solve + walk
        steps 0-4. Returns True on success. Never raises; failures are logged
        and Telegrammed per the failure-mode table (spec §8)."""
        log_line("SEAT_ACQUIRING", f"embassy={EMBASSY_ID}/{EMBASSY_NAME}", elapsed_since_trigger)
        try:
            # Fix 11 (CRITICAL): start the reserve's first solve THE SAME
            # MOMENT this acquisition begins, running in parallel with the
            # acquisition's own CAPTCHA solve below - not after the seat is
            # established, which is what left the reserve cold at exactly
            # the moment the very first booking attempt needed it (see
            # IMPLEMENTATION_CHANGELOG.md §10 for the live timing that
            # exposed this). This is safe to do unconditionally and this
            # early: solve_capsolver() only needs the sitekey + page URL
            # (F4) and has no dependency on the session/seat being
            # established, unlike the still-open R9 concurrency question
            # (§3, round 1) which is specifically about threading the
            # ACQUISITION's own CAPTCHA solve together with session-dependent
            # HTTP steps.
            self.token_reserve.prewarm()

            session = http_flow.new_session()
            has_dvisa, services, _ = check_service_offered(session, EMBASSY_ID)
            if not has_dvisa:
                log_line("INFO", f"D-visa not currently offered (F8) - cannot acquire a seat. Services: {services}")
                return False

            # Round 8: this is THE line the pool exists for. Acquisition needs
            # a token ~1.5s into the flow, far too early to prewarm
            # reactively, so before the pool this always cost a full 15-25s
            # solve and every episode was seen ~20s late.
            captcha_token = self.token_reserve.take()
            if captcha_token is None:
                log_line("INFO", "Token reserve dry at acquisition - solving on demand (~15-25s)")
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
            log_line(
                "SEAT_ACQUIRED",
                f"dates_seen={seat.available_dates} age=0s requests_used={seat.requests_used}",
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
        """R10/R11: start an overlapping renewal before the current seat dies.
        R12: only renew if D-visa is still offered - otherwise let the old
        seat expire and go back to waiting for a trigger (checked inside
        begin_overlapping_reacquire). R14: at most one renewal/reacquisition
        in flight at a time, shared with round-3's suspect-driven
        reacquisition (Fix 9).

        Round-5 Fix 16: renew on EITHER request count OR elapsed time,
        whichever comes first.

        Time alone was wrong. Spec F5 recorded a seat as lasting 1440s, but
        three live runs at three different poll cadences all died at ~95-100
        REQUESTS regardless of how long that took: ~98 requests at 1442s when
        polling every 15s, ~100 requests at 190s when polling every 2s
        (five consecutive times, 189-192s), ~95 requests at 138s with date
        refreshes on top. The 1440s figure was an artifact of the original
        measurement's 15s interval. At the daemon's 2s cadence a purely
        time-based threshold of 1200s never fires, so every seat died
        unrenewed and left a ~37s coverage gap.

        Both conditions are kept deliberately rather than replacing one with
        the other: the request-count evidence is strong but comes from one
        embassy over one afternoon, and a genuine time cap may also exist
        that we simply never isolated. Whichever binds first wins.
        """
        if not self.has_live_seat():
            return
        used = self.seat.requests_used
        age = self.seat.age_seconds()
        if used >= SEAT_RENEW_AFTER_REQUESTS:
            self.begin_overlapping_reacquire(reason=f"request_budget({used}/{SEAT_RENEW_AFTER_REQUESTS})")
            return
        if age >= SEAT_RENEW_AFTER_SECONDS:
            self.begin_overlapping_reacquire(reason=f"ttl_renewal({age:.0f}s)")

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
                    f"reason={reason} old_seat_age={self.seat.age_seconds():.0f}s "
                    f"old_seat_requests={self.seat.requests_used}" if self.seat else f"reason={reason}",
                )
                # Fix 11: applies here too, per round 4's instructions - a
                # swapped-in seat should also arrive with a warm reserve
                # rather than relying solely on whatever the (shared,
                # continuously-running) reserve happens to already hold.
                # Harmless/free if the reserve is already mid-solve or
                # already holds a fresh token - _solve_and_store()'s
                # _resolve_in_progress guard dedupes, and _has_fresh_token()
                # is checked before any redundant periodic solve too.
                self.token_reserve.prewarm()
                session = http_flow.new_session()
                has_dvisa, services, _ = check_service_offered(session, EMBASSY_ID)
                if not has_dvisa:
                    log_line(
                        "INFO",
                        f"R12: D-visa no longer offered - skipping {reason} reacquire, keeping current seat. Services: {services}",
                    )
                    return
                captcha_token = self.token_reserve.take()
                if captcha_token is None:
                    log_line("INFO", f"Token reserve dry at {reason} reacquire - solving on demand (~15-25s)")
                    captcha_token = solve_on_demand()
                applicant = load_applicant_data()
                new_seat = establish_seat(EMBASSY_ID, applicant, captcha_token, session=session)
                cache_applicant_on_seat(new_seat, applicant)
                old_age = self.seat.age_seconds() if self.seat else None
                old_used = self.seat.requests_used if self.seat else None
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
                    f"reason={reason} old_seat_age={old_age:.0f}s old_seat_requests={old_used} "
                    f"new_dates={new_seat.available_dates} new_seat_requests={new_seat.requests_used}",
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
            log_line(
                "SEAT_EXPIRED",
                f"age={self.seat.age_seconds():.0f}s > SEAT_TTL_SECONDS={SEAT_TTL_SECONDS} "
                f"requests_used={self.seat.requests_used}",
            )
            self.drop_seat()

    def begin_background_acquire(self, reason: str) -> bool:
        """Round-5 Fix 17: acquire a replacement seat off the main thread
        after the current one has already died.

        Distinct from `begin_overlapping_reacquire()`, which requires a live
        seat to overlap with - by the time this is called there is nothing
        left to overlap. Before this existed, a dead seat simply waited for
        the monitor's next trigger before being replaced, which produced ~37s
        of zero coverage after every death (observed five times in one 30-min
        run, /tmp/daemon_t5.log). Since seats now die roughly every ~190s at
        the 2s poll cadence (Fix 16), those gaps would have been a permanent
        feature of normal operation, not an edge case.

        Deliberate deviation from the round-5 instruction to reuse
        REACQUIRE_COOLDOWN_SECONDS (300s) here: seat death is EXPECTED every
        ~190s, so a 5-minute cooldown would leave the daemon seatless most of
        the time - it would convert a 37s gap into a 110s one. A short
        dedicated cooldown prevents a tight spin if acquisition fails
        immediately, while MAX_SOLVES_PER_HOUR (60/hr, versus the ~19/hr this
        implies) remains the real spend backstop.
        """
        now = time.time()
        if (now - self.last_death_reacquire_ts) < SEAT_DEATH_REACQUIRE_COOLDOWN_SECONDS:
            return False
        with self._renewal_lock:
            if self._renewal_in_progress:
                return False
            self._renewal_in_progress = True
        self.last_death_reacquire_ts = now

        def _run():
            try:
                log_line("SEAT_ACQUIRING", f"reason={reason} (replacing a dead seat)")
                # acquire() sets self.seat itself and does its own D-visa
                # gate check (F8), so a withdrawn service is handled there.
                self.acquire()
            except Exception as e:
                log_line("INFO", f"Background reacquire after seat death failed: {e}")
            finally:
                with self._renewal_lock:
                    self._renewal_in_progress = False

        threading.Thread(target=_run, daemon=True).start()
        return True

    def drop_seat(self) -> None:
        self.seat = None
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

# Round-4 Fix 14: DAEMON_DRY_RUN=1 must attempt a booking only once. Abu
# Dhabi-style standing availability means every poll cycle finds a slot, so
# without this the daemon re-ran the ENTIRE booking path - including an
# on-demand CAPTCHA solve whenever the reserve happened to be empty - on
# every single cycle forever (~12 solves observed in a 5-minute dry run).
# Module-level (not per-SeatManager) because DAEMON_DRY_RUN is a whole-process
# mode, not a per-seat property, and must stay set across any seat swap.
_dry_run_completed = threading.Event()


def attempt_booking(seat_mgr: SeatManager, date_iso: str, slot: Slot, elapsed_since_trigger: Optional[float]) -> bool:
    """Returns True if the daemon should stop all further booking attempts
    (i.e. a confirmed booking happened, checked via the persistent flag on
    the NEXT poll_cycle() call - see below re: Fix 13's async path). R25:
    check the booked flag right before every submit, not just at startup - a
    race against the old Playwright booker or a human is possible and this
    is the last line of defence against double-booking."""
    if not should_book_date(date_iso):
        # Round-6: not "already booked" but "this would not be an
        # improvement". Returning True stops this poll cycle's date loop,
        # which is correct: rank_dates() is earliest-first, so every
        # remaining date is later still.
        held = currently_booked_date()
        log_line("INFO", f"{date_iso} is not earlier than the held booking ({held}) - not rebooking")
        return True

    if DAEMON_DRY_RUN and _dry_run_completed.is_set():
        # Fix 14: one full dry-run pass (through to "would have booked",
        # inside _finish_booking below) has already happened. Keep polling
        # and logging POLL_RESULT/SLOT_FOUND normally (unaffected - this
        # function just isn't called with any further effect) but spend no
        # more CAPTCHA solves or Telegram noise on it.
        return False

    with seat_mgr.booking_lock:
        if seat_mgr.booking_in_progress:
            # Fix 13: a previous booking attempt (on-demand solve and/or the
            # eventual submit) is still in flight in the background. Do not
            # dispatch a second, concurrent one - "keep the booking attempt
            # itself sequential and correct" per round 4's instructions.
            log_line(
                "INFO",
                "A booking attempt is already in flight for this seat - not starting a second one "
                "(will be tried again next poll cycle if it fails)",
                elapsed_since_trigger,
            )
            return False
        seat_mgr.booking_in_progress = True

    log_line(
        "BOOKING_ATTEMPT",
        f"date={date_iso} time={slot.time} slot_id={slot.value} dry_run={DAEMON_DRY_RUN}",
        elapsed_since_trigger,
    )

    seat = seat_mgr.seat
    if seat is None or not seat.alive:
        log_line("BOOKING_FAILED", "no live seat at submit time", elapsed_since_trigger)
        with seat_mgr.booking_lock:
            seat_mgr.booking_in_progress = False
        return False

    # R23: use the reserve; if empty/stale, solve on demand.
    captcha_token = seat_mgr.token_reserve.take()
    if captcha_token is not None:
        # Fast path - Fix 11 makes this the common case now. Proceed
        # synchronously, exactly as before Fix 13.
        return _finish_booking(seat_mgr, date_iso, slot, captcha_token, elapsed_since_trigger)

    # Fix 13: the reserve was empty/stale. Previously `solve_on_demand()` ran
    # right here, blocking the ENTIRE main loop (trigger detection, other
    # dates' polling, everything) for the full ~15-25s solve - observed
    # directly as a total gap in POLL_RESULT lines (17:38:33 -> 17:38:53, see
    # IMPLEMENTATION_CHANGELOG.md §10). The solve itself touches no session
    # state at all (F4 - CapSolver only needs the sitekey + page URL), so it
    # is safe to run off the main thread; only the eventual submit (inside
    # _finish_booking) needs to be serialized against a concurrent
    # free-times query on the SAME session (R16), via seat_mgr.session_lock.
    log_line(
        "INFO",
        "Token reserve empty/stale at booking time - solving on demand in the background so polling "
        "continues (Fix 13; still costs ~15-25s, but no longer stalls the poll loop)",
        elapsed_since_trigger,
    )

    def _background_solve_and_finish():
        try:
            token = solve_on_demand()
        except Exception as e:
            log_line("BOOKING_FAILED", f"on-demand CAPTCHA solve failed: {e}", elapsed_since_trigger)
            send_telegram(
                f"❌ Slot found for {EMBASSY_NAME} on {date_iso} {slot.time} but the booking "
                f"CAPTCHA could not be solved: {e}\nCheck the portal manually.{RUNTIME_SIGNATURE}"
            )
            with seat_mgr.booking_lock:
                seat_mgr.booking_in_progress = False
            return
        _finish_booking(seat_mgr, date_iso, slot, token, elapsed_since_trigger)

    threading.Thread(target=_background_solve_and_finish, daemon=True).start()
    # Not confirmed synchronously. poll_cycle() treats False as "try the next
    # date this cycle" (R22) / "try again next cycle" - the persistent
    # booked-flag check at the top of poll_cycle()/attempt_booking() is what
    # actually stops further attempts once _finish_booking confirms a
    # booking asynchronously (within one SLOT_POLL_INTERVAL_SECONDS of it
    # happening - the existing R25 check, unchanged).
    return False


def _finish_booking(seat_mgr: SeatManager, date_iso: str, slot: Slot, captcha_token: str, elapsed_since_trigger: Optional[float]) -> bool:
    """The part of a booking attempt that needs an actual CAPTCHA token in
    hand: the DAEMON_DRY_RUN stop-before-submit branch (R26/Fix 14), or the
    real submit (R19-R25). Always clears seat_mgr.booking_in_progress on the
    way out (Fix 13), whether called synchronously (reserve had a token) or
    from the Fix-13 background thread (reserve was empty)."""
    try:
        if not should_book_date(date_iso):
            # Re-check after the wait: a solve takes ~15-25s, during which
            # another path (the old Playwright booker, a human, or a raced
            # second attempt) may have booked this date or something better.
            log_line("INFO", f"{date_iso} stopped being an improvement while waiting for a token - skipping submit")
            return True

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
            _dry_run_completed.set()  # Fix 14: no further booking attempts for the rest of this run
            return False

        seat = seat_mgr.seat
        if seat is None or not seat.alive:
            log_line("BOOKING_FAILED", "no live seat at submit time (died while solving/waiting for a token)", elapsed_since_trigger)
            return False

        try:
            applicant = load_applicant_data()
            # Fix 13: the only line in this whole function that actually
            # touches the seat's live session - held only for this one
            # request, so a concurrent free-times query (R16) waits at most
            # a sub-second beat, never the full CAPTCHA-solve duration.
            with seat_mgr.session_lock:
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
            # Dump the confirmation page on SUCCESS too, not just on an
            # unclear outcome. On 2026-08-03 a real submit produced an
            # unverifiable result and there was no artifact of any kind to
            # inspect afterwards; a confirmed booking is exactly when we most
            # want a record of what the server actually said.
            confirm_dump = dump_html(f"booking_confirmed_{EMBASSY_ID}", response_html)
            delete_href = find_delete_link(response_html)
            details = {
                "date": date_iso, "time": slot.time, "slot_id": slot.value, "embassy": EMBASSY_ID,
                "delete_url": (f"https://broneering.mfa.ee{delete_href}" if delete_href else None),
                "confirmation_dump": confirm_dump,
            }
            previous = mark_booked(details)
            took = f"~{elapsed_since_trigger:.1f}s from slot detection to submit." if elapsed_since_trigger else ""
            cancel = details.get("delete_url") or "(not found on the confirmation page)"
            when = f"{date_iso} at {slot.time}"
            speed = (
                f"Booked {elapsed_since_trigger:.1f}s after the slot was detected."
                if elapsed_since_trigger else "Booked."
            )
            if previous:
                # Round-6/9: an improvement, not a first booking. The older
                # appointment is still live and only the owner can remove it,
                # so this has to be impossible to skim past.
                send_telegram(
                    f"✅ BOOKED AN EARLIER APPOINTMENT\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Embassy:  Estonia, {EMBASSY_NAME}\n"
                    f"Date:     {when}\n"
                    f"Applicant: {applicant.get('first_name','')} {applicant.get('surname','')}\n"
                    f"Ref:      {applicant.get('visa_application_number','-')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{speed}\n\n"
                    f"⚠️ ACTION NEEDED — YOU NOW HOLD TWO APPOINTMENTS.\n"
                    f"Delete the older one ({previous}) yourself on the portal. This system "
                    f"will never delete an appointment.\n\n"
                    f"Cancel link for the NEW booking (keep, do not click unless undoing):\n{cancel}\n\n"
                    f"Still hunting for anything earlier than {date_iso}.{RUNTIME_SIGNATURE}"
                )
            else:
                send_telegram(
                    f"✅ APPOINTMENT BOOKED\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Embassy:  Estonia, {EMBASSY_NAME}\n"
                    f"Date:     {when}\n"
                    f"Applicant: {applicant.get('first_name','')} {applicant.get('surname','')}\n"
                    f"Ref:      {applicant.get('visa_application_number','-')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{speed}\n\n"
                    f"Check your email for the portal's own confirmation.\n\n"
                    f"Cancel link (keep this, do not click unless undoing):\n{cancel}\n\n"
                    f"The daemon keeps running and will automatically rebook if an EARLIER "
                    f"date appears — you would then delete this one manually.{RUNTIME_SIGNATURE}"
                )
            return True
        else:
            # Spec §8: "Booking submitted, confirmation unclear -> Do NOT set
            # the booked flag. Telegram a warning telling the owner to check
            # the portal manually." Never silently swallow an unclear outcome.
            dump_path = dump_html(f"booking_unclear_{EMBASSY_ID}", response_html)
            log_line("BOOKING_FAILED", f"submit response did not confirm success - dump: {dump_path}", elapsed_since_trigger)
            send_telegram(
                f"⚠️ Submitted a booking for {EMBASSY_NAME} ({date_iso} {slot.time}) but could not confirm "
                f"success from the response. CHECK THE PORTAL MANUALLY - it may or may not have gone "
                f"through. (booked flag NOT set){RUNTIME_SIGNATURE}"
            )
            return False
    finally:
        with seat_mgr.booking_lock:
            seat_mgr.booking_in_progress = False


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
        # Round-7: back right off while the calendar is empty, to preserve the
        # seat's ~100-request budget for the episode that matters.
        interval = (
            IDLE_DATE_REFRESH_INTERVAL_SECONDS if not seat.available_dates
            else DATE_REFRESH_INTERVAL_SECONDS
        )
        if (now - seat_mgr.last_date_refresh_ts) < interval:
            return False  # not due yet - reuse the cached date list, no request made

    q0 = time.time()
    try:
        # Fix 13: serialize against a concurrent booking submit on the same
        # session (R16) - see seat_mgr.session_lock's definition.
        with seat_mgr.session_lock:
            new_dates = resolve_r17_refresh_dates(seat)
    except SeatDeadError as e:
        handle_seat_death(seat_mgr, e, "detected during R17 date-list refresh", elapsed_since_trigger)
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
    append_poll_csv(seat.age_seconds(), "__DATE_REFRESH__", len(new_dates), new_dates, query_ms,
                    seat.alive, seat.requests_used)
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


def handle_seat_death(seat_mgr: "SeatManager", exc: SeatDeadError, where: str,
                      elapsed_since_trigger: Optional[float]) -> None:
    """Round-5 Fixes 17+18: single place that reacts to a seat dying.

    Fix 18 - dump the response body. Seat death has only ever been INFERRED
    from a selector signature (CSRF token present, `broneering[kuupaev]`
    absent); nobody has looked at what the server actually sends. A
    validation-error page would match that same signature, so without a dump
    we cannot distinguish a genuine session expiry from a misread error page.
    One real dump settles it.

    Fix 17 - start a replacement immediately instead of waiting for the
    monitor's next trigger.
    """
    seat = seat_mgr.seat
    used = seat.requests_used if seat is not None else -1
    age = seat.age_seconds() if seat is not None else -1.0
    path = dump_html("seat_died", getattr(exc, "html", "") or "(no body captured)")
    log_line(
        "SEAT_DIED",
        f"{where} after requests_used={used} age={age:.0f}s "
        f"(renew threshold was {SEAT_RENEW_AFTER_REQUESTS} requests / "
        f"{SEAT_RENEW_AFTER_SECONDS:.0f}s) - dump: {path}",
        elapsed_since_trigger,
    )
    seat_mgr.drop_seat()
    # Round-6: always replace. The daemon no longer stops after a booking -
    # it keeps hunting for an earlier date indefinitely.
    seat_mgr.begin_background_acquire(reason="seat_died")


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

    any_slots_this_cycle = False
    for date_iso in rank_dates(seat.available_dates):
        q0 = time.time()
        try:
            # Fix 13: serialize against a concurrent booking submit on the
            # same session (R16) - see seat_mgr.session_lock's definition.
            # This is the only new source of contention Fix 13 introduces:
            # if a background booking submit is mid-flight, this query waits
            # for that single, sub-second HTTP call to finish, not the whole
            # ~15-25s CAPTCHA solve that used to precede it.
            with seat_mgr.session_lock:
                slots = query_free_times(seat, date_iso)
        except SeatDeadError as e:
            handle_seat_death(seat_mgr, e, f"detected while querying {date_iso}", elapsed_since_trigger)
            return False
        except Exception as e:
            log_line("INFO", f"Query failed for {date_iso} (non-fatal, will retry next cycle): {e}")
            continue
        query_ms = (time.time() - q0) * 1000
        times = [s.time for s in slots]
        append_poll_csv(seat.age_seconds(), date_iso, len(slots), times, query_ms,
                        seat.alive, seat.requests_used)
        log_line("POLL_RESULT", f"date={date_iso} slots={len(slots)} times={times} query_ms={query_ms:.0f}")

        if not slots:
            continue  # R18: normal, keep going

        # Fix 7: any non-empty result anywhere in this cycle immediately
        # resets the empty-poll streak - the seat is demonstrably able to
        # see real inventory, which is the opposite of suspect.
        any_slots_this_cycle = True
        seat_mgr.empty_poll_streak = 0

        log_line("SLOT_FOUND", f"date={date_iso} times={times}", elapsed_since_trigger)
        notify_available_slots(date_iso, times, currently_booked_date())
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
# Round-4 Fix 15 (minor): episode-based elapsed_since_trigger
# ---------------------------------------------------------------------------

def update_episode_start(episode_start_ts: Optional[float], previous_last_trigger_ts: float, new_trigger_ts: float) -> float:
    """Every `elapsed_since_trigger` logged throughout this file used to be
    computed against the MOST RECENT trigger-file timestamp. But the monitor
    hook writes a fresh trigger on every ~10s detection tick for an entire
    ~90-110s episode (not once per episode - see IMPLEMENTATION_CHANGELOG.md
    §9), so "elapsed since the latest trigger" reset to ~0.00s on every one
    of those re-announcements - it was measuring time since the last
    re-announcement, not since the episode (and therefore the real
    opportunity to book) began. Since round 4's whole point is judging real
    end-to-end latency (Fix 11's ~21s cold-path target), this number has to
    mean what it says.

    A new episode is defined the same way Fix 7 already defines "no longer
    fresh": a gap of more than TRIGGER_FRESH_WINDOW_SECONDS with no trigger
    at all. Reusing that existing constant rather than adding a new one -
    the two concepts (is a trigger still fresh enough to corroborate
    inventory, is this a new episode) are naturally the same threshold.

    Returns the episode-start timestamp to use from here on: unchanged if
    this trigger continues an episode already in progress, or reset to
    `new_trigger_ts` if this is the first trigger of a new one.
    """
    is_new_episode = (previous_last_trigger_ts <= 0) or ((new_trigger_ts - previous_last_trigger_ts) > TRIGGER_FRESH_WINDOW_SECONDS)
    if is_new_episode or episode_start_ts is None:
        return new_trigger_ts
    return episode_start_ts


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
        f"Daemon starting. embassy={EMBASSY_ID}/{EMBASSY_NAME} profile={BOOKING_PROFILE or '(default)'} "
        f"dry_run={DAEMON_DRY_RUN} self_detect={DAEMON_SELF_DETECT} captcha_provider={CAPTCHA_PROVIDER} "
        f"state_file={BOOKED_STATE_FILE.name} applicant_data={REAL_DATA_PATH.name}",
    )
    send_telegram(
        f"🚀 Estonia D-visa booker daemon started for {EMBASSY_NAME}"
        f"{' (DRY RUN)' if DAEMON_DRY_RUN else ''}.{RUNTIME_SIGNATURE}"
    )

    seat_mgr = SeatManager()
    trigger_watcher = TriggerWatcher()

    # Round 8: start filling the token pool immediately, before any trigger.
    # This is the whole point of the change - a pool that only fills once a
    # seat exists cannot help the seat get created, and seat acquisition is
    # exactly where the 20-second CAPTCHA cost was being paid.
    if TOKEN_RESERVE_SIZE < 2:
        log_line(
            "INFO",
            f"WARNING: TOKEN_RESERVE_SIZE={TOKEN_RESERVE_SIZE}. With fewer than 2 tokens, "
            f"acquisition consumes the only one and the submit ~2s later still waits "
            f"15-25s for a fresh solve - which is the exact failure this pool exists to remove.",
        )
    seat_mgr.token_reserve.start()

    trigger_ts: Optional[float] = None  # latest trigger seen (Fix 7's freshness checks use seat_mgr.last_trigger_ts, same value)
    episode_start_ts: Optional[float] = None  # Fix 15: first trigger of the CURRENT episode - see update_episode_start()
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

                # Round-6b: discard a trigger file left over from a PREVIOUS
                # episode before acting on it. On 2026-08-03 07:20:21 the
                # daemon started up, read a 209-second-old trigger, tried to
                # acquire a seat, and got "D-visa not currently offered" -
                # that episode had ended four minutes earlier. Harmless then,
                # but it would waste a CAPTCHA the moment the timing lined up
                # differently. Filtered here, before the handling block, so
                # nothing downstream has to cope with a half-processed trigger.
                if trigger is not None:
                    _age = time.time() - trigger["ts"]
                    if _age > TRIGGER_FRESH_WINDOW_SECONDS:
                        log_line(
                            "INFO",
                            f"Ignoring stale trigger ({_age:.0f}s old, older than "
                            f"TRIGGER_FRESH_WINDOW_SECONDS={TRIGGER_FRESH_WINDOW_SECONDS:.0f})",
                        )
                        seat_mgr.last_trigger_ts = trigger["ts"]
                        trigger = None

                if trigger is not None:
                    new_trigger_ts = trigger["ts"]
                    # Fix 15: figure out episode boundaries BEFORE
                    # overwriting seat_mgr.last_trigger_ts below - a "new
                    # episode" is judged against the PREVIOUS latest trigger,
                    # not this one.
                    episode_start_ts = update_episode_start(episode_start_ts, seat_mgr.last_trigger_ts, new_trigger_ts)
                    trigger_ts = new_trigger_ts
                    elapsed = time.time() - episode_start_ts
                    log_line("TRIGGER_RECEIVED", f"services={trigger.get('services')}", elapsed)
                    # Fix 7: record that a trigger was just seen, regardless
                    # of seat state - this is the "inventory exists right
                    # now, independently confirmed" signal
                    # check_suspect_and_maybe_reacquire() checks freshness
                    # against. Uses the trigger's own timestamp (when the
                    # monitor actually detected availability), not the time
                    # the daemon happened to notice the file. Deliberately
                    # the LATEST trigger, not the episode start (Fix 15) -
                    # freshness and "how long has this episode been running"
                    # are different questions.
                    seat_mgr.last_trigger_ts = new_trigger_ts
                    if not seat_mgr.has_live_seat():
                        seat_mgr.acquire(elapsed_since_trigger=elapsed)
                    elif seat_mgr.has_live_seat():
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
                        # Fix 15: episode-based, not latest-trigger-based -
                        # see update_episode_start()'s docstring.
                        elapsed = (time.time() - episode_start_ts) if episode_start_ts is not None else None
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
