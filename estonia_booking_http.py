#!/usr/bin/env python3
"""
Reusable, browserless HTTP implementation of the broneering.mfa.ee booking
flow, extracted from the proven, measured code in `probe_http_flow.py` and
`test_session_longevity.py` (see `SPEC - Parked Session Booker Daemon.md`,
sections 5 and 11 step 1).

This module intentionally contains NO polling loop, NO trigger-file watching,
and NO Telegram/logging side effects of its own - it is pure HTTP-flow
plumbing. `estonia_booker_daemon.py` is the only caller and owns all
scheduling, state, and alerting decisions. Keeping this file side-effect-free
(besides the network calls it obviously has to make) makes it directly
unit-testable and reusable from `probe_http_flow.py`-style throwaway scripts
too, without dragging in daemon concerns.

ROUND-2 FOLLOW-UP FIX 3 - ONE DELIBERATE EXCEPTION TO "no side effects of its
own": this module now owns a single, global, thread-safe rate limiter
(`RateLimiter`, singleton `_RATE_LIMITER`) that EVERY outbound request to
broneering.mfa.ee passes through via the `_throttled()` helper - see
IMPLEMENTATION_CHANGELOG.md §8 (Fix 3) for the full rationale. This is a
safety net, not a tuning knob: no future bug in any call path (daemon,
`probe_http_flow.py`, a throwaway script, anything importing this module) can
produce runaway traffic against a site that has already added a CAPTCHA in
response to automation. Because this lives below the daemon's own logging
(estonia_booker_daemon.py imports this module, so the reverse import would be
circular), a minimal standalone `_log()` helper was added here purely to emit
the `RATE_THROTTLED` line the fix requires - everything else in this module
remains free of logging/Telegram side effects as originally documented.

Every fact this module encodes is measured, not assumed - see the spec's
"Confirmed facts" (F1-F10) section before changing any of the request shapes
below. In particular:

  F1  The whole flow is plain Symfony form POSTs. No browser required.
  F2  Both reCAPTCHAs are enforced server-side - there is no bypass.
  F5  A session lasts exactly 1440s and does not extend on activity. Death
      signature: response has a CSRF token but no
      `input[name="broneering[kuupaev]"]` (R13).
  F6  Once past the CAPTCHA gate, a free-times query needs no CAPTCHA and is
      fast (~200-600ms).

R17 (this module's open question - see `resolve_r17_refresh_dates` below):
can the available-dates list be refreshed inside a live seat without a new
CAPTCHA? This module implements the spec's suggested no-CAPTCHA route
(re-POSTing the applicant-details step, which carries no
`g-recaptcha-response` field at all) and documents in
`IMPLEMENTATION_CHANGELOG.md` §4 that this could not be verified against the
live site in the implementation session (no network egress to
broneering.mfa.ee was available in that sandbox) - it must be verified for
real before being trusted, exactly as the spec's "stop and flag" instruction
in §11 step 3 requires.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    # Same guard as estonia_visa_monitor.py / probe_http_flow.py - a missing
    # optional dependency must never be an outage (see AI_CONTEXT.md §10.4,
    # the 45-minute monitor outage this caused once already).
    HTML_PARSER = "html.parser"

URL = "https://broneering.mfa.ee/en/"
SERVICE_VALUE = "pikaajaline_viisa"
RECAPTCHA_SITEKEY = "6Ld04R4nAAAAADJhUQpKyGxGh4zlfOsyk5VkVkzR"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TURBO_HEADERS = {
    "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": URL,
    "Origin": "https://broneering.mfa.ee",
}
# `edasi` / `broneeri` steps override Accept per spec §5.
FULL_HEADERS = {**TURBO_HEADERS, "Accept": "text/html, application/xhtml+xml"}


# ---------------------------------------------------------------------------
# Round-2 follow-up Fix 3: a hard, global outbound rate ceiling
# ---------------------------------------------------------------------------
#
# Every request this module makes to broneering.mfa.ee - across every call
# site (check_service_offered, establish_seat, resolve_r17_refresh_dates,
# query_free_times, submit_booking, delete_booking) and every caller
# (the daemon's main thread AND its background seat-renewal thread, which run
# concurrently) - passes through `_RATE_LIMITER.acquire()` via the
# `_throttled()` helper below. This is intentionally the lowest possible
# choke point: no future bug in scheduling logic elsewhere can bypass it,
# because there is no other way to reach the site from this module.


def _log(msg: str) -> None:
    """Minimal standalone logger - see the module docstring's "ROUND-2
    FOLLOW-UP FIX 3" note for why this one function exists despite the rest
    of the module deliberately having no logging side effects."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} [estonia_booking_http] {msg}", flush=True)


class RateLimiter:
    """Thread-safe token-bucket limiter. `acquire()` BLOCKS until a token is
    available - it never drops or rejects a request, per the fix's explicit
    instruction ("Do NOT drop requests"). `clock`/`sleep_fn` are injectable
    solely so this can be unit-tested with a fake clock and zero real
    waiting (see test_daemon_units.py); production code always uses the
    defaults (`time.monotonic` / `time.sleep`).
    """

    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        log_interval_seconds: float = 30.0,
    ):
        # A rate of exactly 0 would divide-by-zero below and would also mean
        # "never allow any request", which is not what a misconfigured 0
        # should silently do to a safety net - floor it to something tiny
        # but nonzero instead of either crashing or silently blocking forever
        # in a way that looks like a hang rather than a config error.
        self.rate = max(float(rate_per_second), 1e-6)
        self.capacity = float(burst)
        self.tokens = float(burst)
        self._clock = clock
        self._sleep = sleep_fn
        self._last_refill = clock()
        self._lock = threading.Lock()
        self._log_interval = log_interval_seconds
        self._last_throttle_log = 0.0
        self._granted = 0
        self._first_grant_ts: Optional[float] = None

    def _refill_locked(self) -> None:
        """Caller must hold self._lock."""
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def measured_rate(self) -> float:
        """Average granted-requests-per-second since the first grant. This is
        an average since the limiter started, not an instantaneous rate -
        good enough for a log line meant to show "yes, throttling is
        actually keeping this near the configured ceiling", not for
        anything load-bearing."""
        with self._lock:
            if self._first_grant_ts is None:
                return 0.0
            elapsed = max(self._clock() - self._first_grant_ts, 1e-6)
            return self._granted / elapsed

    def acquire(self) -> None:
        while True:
            should_log = False
            wait_time = 0.0
            measured = 0.0
            with self._lock:
                self._refill_locked()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self._granted += 1
                    if self._first_grant_ts is None:
                        self._first_grant_ts = self._clock()
                    return
                deficit = 1.0 - self.tokens
                wait_time = deficit / self.rate
                now = self._clock()
                if (now - self._last_throttle_log) >= self._log_interval:
                    self._last_throttle_log = now
                    should_log = True
            if should_log:
                # Computed outside the lock (measured_rate() takes it itself)
                # to avoid a nested-lock deadlock risk if this were ever
                # changed to a non-reentrant lock type elsewhere.
                measured = self.measured_rate()
                _log(
                    f"RATE_THROTTLED waiting {wait_time:.2f}s for a token "
                    f"(measured rate so far: {measured:.2f} req/s, "
                    f"configured limit: {self.rate:.2f} req/s, burst={int(self.capacity)})"
                )
            self._sleep(wait_time)


# Round 10: burst raised 3 -> 10. The SUSTAINED rate is unchanged at 1 req/s,
# so the load this places on the site is identical - but seat acquisition
# fires 7 requests back to back, and with a burst of 3 the limiter was
# throttling the one moment that matters. Measured on 2026-08-03: a
# request-budget renewal took 10s wall-clock (08:07:52 -> 08:08:02) with
# RATE_THROTTLED lines in the middle, despite the CAPTCHA being instant from
# the pool. A burst of 10 lets a single acquisition through untouched while
# the token bucket still refills at 1/s, so nothing sustained changes.
_RATE_LIMITER = RateLimiter(
    rate_per_second=float(os.environ.get("MAX_REQUESTS_PER_SECOND", "1.0")),
    burst=int(os.environ.get("REQUEST_BURST_ALLOWANCE", "10")),
)


def _throttled(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Blocks on the global rate limiter, then calls fn(*args, **kwargs).
    Every `session.get(...)`/`session.post(...)` call to the live site in
    this module goes through this - see the section comment above.

    Round-5 Fix 16: this is also where per-session request counting happens.
    Measurements on 2026-08-02 (three runs at three different poll cadences)
    showed a session expires after roughly 95-100 REQUESTS, not after any
    fixed wall-clock duration - the original "1440 seconds" figure turned out
    to be an artifact of that one measurement having polled every 15s
    (~98 requests happens to land at ~1442s). See IMPLEMENTATION_CHANGELOG.md
    §11 and AI_CONTEXT.md F5's correction.

    Counting here rather than at each call site is deliberate: this function
    is already the single unbypassable chokepoint for site traffic (Fix 3),
    so a request cannot be made without being counted. The counter lives on
    the `requests.Session` because that IS the unit the server's budget
    applies to - which also means setup requests (steps 0-4, plus the
    `check_service_offered` probe that shares the same session) are included
    automatically, with no risk of under-counting a seat's true usage.
    """
    _RATE_LIMITER.acquire()
    owner = getattr(fn, "__self__", None)
    if isinstance(owner, requests.Session):
        owner.request_count = getattr(owner, "request_count", 0) + 1
    return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BookingHttpError(RuntimeError):
    """Raised when a step fails in a way that should be treated as a markup
    change / unexpected condition, not routine 'no slots'. Callers (the
    daemon) are expected to dump the offending HTML and alert (R33-R38 /
    the failure-mode table in spec §8: 'Site markup changes')."""

    def __init__(self, step: str, message: str, html: str = ""):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.html = html


class SeatDeadError(RuntimeError):
    """Raised by query_free_times()/refresh_dates() when the seat-death
    signature (R13) is detected: a response still carries a CSRF token but
    the date field (`input[name="broneering[kuupaev]"]`) is gone - i.e. we
    were bounced back to the start of the form (F5).

    Round-5 Fix 18: carries the offending response body. Seat death has only
    ever been *inferred* from that selector signature - nobody has looked at
    what the server actually returns. A validation-error page would match the
    same signature, so a real dump is the only way to tell a genuine session
    expiry from a misread error page. Callers are expected to write
    `.html` to disk (see the daemon's `dump_html`).
    """

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        self.html = html


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    value: str          # internal slot id, e.g. "2249077" - NOT the time
    time: str           # displayed HH:MM, from label[for=checkbox.id]


@dataclass
class Seat:
    """A live, authenticated, parked session sitting on the calendar page.

    One `requests.Session` per seat so cookies persist (spec §5). The CSRF
    token rotates on every response and must be tracked here, not captured
    once.
    """

    session: requests.Session
    embassy_id: str
    token: str
    established_at: float  # time.time() when the calendar was first reached
    available_dates: list[str] = field(default_factory=list)
    last_seen_dates: list[str] = field(default_factory=list)
    alive: bool = True

    def age_seconds(self) -> float:
        return time.time() - self.established_at

    @property
    def requests_used(self) -> int:
        """Round-5 Fix 16: how many site requests this seat's session has
        made, including the ~5 spent establishing it. Read straight off the
        session (incremented in `_throttled`) rather than tracked separately,
        so it cannot drift out of sync with reality.

        This, not `age_seconds()`, is the number that predicts when the seat
        will die: observed ceiling is ~95-100 requests regardless of how long
        that takes to spend.
        """
        return getattr(self.session, "request_count", 0)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def extract_token(html: str) -> Optional[str]:
    """The CSRF token rotates on every response, so it must be re-read from
    each one. Turbo-stream responses wrap the form in a
    <turbo-stream><template>, but the <input name="broneering[_token]"> is
    still a plain input either way, so one selector covers both shapes."""
    soup = BeautifulSoup(html, HTML_PARSER)
    inp = soup.find("input", {"name": "broneering[_token]"})
    if inp and inp.get("value"):
        return inp["value"]
    m = re.search(r'name="broneering\[_token\]"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def _iso_to_ddmmyyyy(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


def is_seat_dead(html: str) -> bool:
    """R13: positive seat-death detection. A live session always echoes a
    fresh CSRF token; a dead one still has a token but has been bounced to
    the start of the form, where the date field no longer exists. Do NOT
    infer death from an empty slot list - that is either a normal poll
    result or an expired seat, and conflating the two defeats the entire
    point of this design (§6.3, R13)."""
    soup = BeautifulSoup(html, HTML_PARSER)
    token = soup.find("input", {"name": "broneering[_token]"})
    date_field = soup.find("input", {"name": "broneering[kuupaev]"})
    has_checkboxes = bool(soup.find_all("input", {"name": "broneering[vastuvotud][]"}))
    return bool(token) and not date_field and not has_checkboxes


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


# ---------------------------------------------------------------------------
# Step 0-4: establish_seat
# ---------------------------------------------------------------------------

def check_service_offered(session: requests.Session, embassy_id: str) -> tuple[bool, list[str], str]:
    """Steps 0-1 only: GET landing + select embassy, then report whether
    D-visa is in the service dropdown. This is the F8 gate check the daemon
    must run before every acquisition/renewal attempt - "you cannot establish
    a session for a service that is not currently offered."

    Returns (has_dvisa, service_labels, token_to_continue_with).
    """
    r = _throttled(session.get, URL, timeout=30)
    r.raise_for_status()
    token = extract_token(r.text)
    if not token:
        raise BookingHttpError("step0_landing", "no CSRF token on landing page", r.text)

    r = _throttled(
        session.post,
        URL,
        headers=TURBO_HEADERS,
        data={
            "broneering[esindus]": embassy_id,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": "",
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "turbo": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, HTML_PARSER)
    sel = soup.find("select", {"name": "broneering[toiming]"}) or soup.find("select", {"id": "broneering_toiming"})
    if not sel:
        raise BookingHttpError("step1_embassy", "service dropdown missing", r.text)
    services = [(o.get("value", "").strip(), o.get_text(strip=True)) for o in sel.find_all("option")]
    token = extract_token(r.text) or token
    has_dvisa = any(v == SERVICE_VALUE for v, _ in services)
    return has_dvisa, [label for _, label in services], token


def establish_seat(
    embassy_id: str,
    applicant: dict[str, str],
    captcha_token: str,
    session: Optional[requests.Session] = None,
) -> Seat:
    """Steps 0-4 of spec §5. Requires an already-solved reCAPTCHA token for
    step 3's gate (F2 - there is no bypass). Raises BookingHttpError with a
    step name on any unexpected markup so the caller can dump+alert (spec §8:
    "Site markup changes"). Raises RuntimeError("no_dvisa") if D-visa is not
    currently offered (F8) - callers must check this before spending a
    CAPTCHA solve.
    """
    s = session or new_session()

    has_dvisa, services, token = check_service_offered(s, embassy_id)
    if not has_dvisa:
        raise RuntimeError(f"no_dvisa: D-visa not currently offered. Services seen: {services}")

    # Step 2: select the service (Turbo refresh, still no CAPTCHA)
    r = _throttled(
        s.post,
        URL,
        headers=TURBO_HEADERS,
        data={
            "broneering[esindus]": embassy_id,
            "broneering[toiming]": SERVICE_VALUE,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": "",
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "turbo": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    token = extract_token(r.text) or token

    # Step 3: CAPTCHA gate -> applicant form
    r = _throttled(
        s.post,
        URL,
        headers=FULL_HEADERS,
        data={
            "broneering[esindus]": embassy_id,
            "broneering[toiming]": SERVICE_VALUE,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": captcha_token,
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "edasi": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, HTML_PARSER)
    name_field = soup.find("input", {"name": "broneering[broneeringuIsikud][0][eesnimi]"})
    token = extract_token(r.text) or token
    if not name_field:
        text = soup.get_text(" ", strip=True)
        raise BookingHttpError(
            "step3_captcha_gate",
            f"applicant form not reached - CAPTCHA rejected or markup changed. Page text: {text[:400]!r}",
            r.text,
        )

    # Step 4: applicant details -> calendar
    r = _throttled(
        s.post,
        URL,
        headers=FULL_HEADERS,
        data={
            "broneering[broneeringuIsikud][0][eesnimi]": applicant["first_name"],
            "broneering[broneeringuIsikud][0][perenimi]": applicant["surname"],
            "broneering[broneeringuIsikud][0][synniaeg]": applicant["date_of_birth"],
            "broneering[broneeringuIsikud][0][telefon]": applicant["phone"],
            "broneering[broneeringuIsikud][0][epost]": applicant["email"],
            "broneering[broneeringuIsikud][0][viisataotluseNumber]": applicant["visa_application_number"],
            "broneering[_token]": token,
            "edasi": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, HTML_PARSER)
    cal = soup.find(attrs={"data-controller": "broneering"})
    token = extract_token(r.text) or token
    if not cal:
        raise BookingHttpError("step4_applicant", "calendar not reached after applicant submit", r.text)

    raw_dates = cal.get("data-vastuvotukuupaevad")
    dates = json.loads(raw_dates) if raw_dates else []

    seat = Seat(
        session=s,
        embassy_id=embassy_id,
        token=token,
        established_at=time.time(),
        available_dates=dates,
        last_seen_dates=dates,
    )
    return seat


# ---------------------------------------------------------------------------
# R17: refreshing the available-dates list within a live seat, no CAPTCHA
# ---------------------------------------------------------------------------

def resolve_r17_refresh_dates(seat: Seat) -> list[str]:
    """
    Attempts to refresh `available_dates` on an already-established seat
    WITHOUT spending a new CAPTCHA. This directly answers spec R17: a seat
    acquired during episode 1 must be able to see a *new* date that appears
    in episode 2, or the whole "one seat covers several episodes" design
    (F9) doesn't actually work for dates outside what was visible at
    acquisition time.

    Approach taken (per spec §6.4's suggestion): re-POST the applicant
    -details step (step 4). That endpoint carries no `g-recaptcha-response`
    field at all in the original flow - it is a POST-only-effect operation
    downstream of the CAPTCHA gate, not gated by one itself - so replaying it
    with the same applicant data should be a legitimate, no-CAPTCHA way to
    re-render the calendar element and read a fresh
    `data-vastuvotukuupaevad` attribute, if the site recomputes it on every
    render (as opposed to baking it in once at session start).

    IMPORTANT - not verified against the live site in the implementation
    session: this sandbox has no network egress to broneering.mfa.ee (see
    IMPLEMENTATION_CHANGELOG.md §4 for the specific evidence). The seat-death
    check (`is_seat_dead`) IS still applied to whatever comes back, so a
    dead seat is correctly detected even if the refresh mechanism itself
    turns out not to work as designed - but whether this actually returns
    an *updated* date list (vs. just the same one, or a rejected/garbled
    response) must be confirmed with a real seat before being trusted
    operationally. If live testing shows this does not refresh the list,
    the fallback documented in the spec is a plain `GET /en/` within the
    session - try that next; note that a plain GET returns a fresh landing
    page (pre-embassy-selection) and its calendar data would need to be
    re-derived by replaying steps 1-2 without a CAPTCHA (both are
    CAPTCHA-free per F2/§5), which is a heavier fallback than the step-4
    replay attempted here.
    """
    applicant_replay = getattr(seat, "_applicant_cache", None)
    if applicant_replay is None:
        # Caller (daemon) is expected to have stashed applicant data on the
        # seat via establish_seat's caller; if not available we can't safely
        # replay step 4 (would need real applicant fields) - raise so the
        # caller knows to skip this optimization for this seat.
        raise RuntimeError(
            "resolve_r17_refresh_dates: no cached applicant payload on this Seat; "
            "call `cache_applicant_on_seat(seat, applicant)` right after establish_seat()"
        )

    r = _throttled(
        seat.session.post,
        URL,
        headers=FULL_HEADERS,
        data={
            "broneering[broneeringuIsikud][0][eesnimi]": applicant_replay["first_name"],
            "broneering[broneeringuIsikud][0][perenimi]": applicant_replay["surname"],
            "broneering[broneeringuIsikud][0][synniaeg]": applicant_replay["date_of_birth"],
            "broneering[broneeringuIsikud][0][telefon]": applicant_replay["phone"],
            "broneering[broneeringuIsikud][0][epost]": applicant_replay["email"],
            "broneering[broneeringuIsikud][0][viisataotluseNumber]": applicant_replay["visa_application_number"],
            "broneering[_token]": seat.token,
            "edasi": "",
        },
        timeout=30,
    )
    r.raise_for_status()

    if is_seat_dead(r.text):
        seat.alive = False
        raise SeatDeadError(
            f"seat died during R17 date-refresh (step-4 replay) after "
            f"{seat.requests_used} requests / {seat.age_seconds():.0f}s",
            html=r.text,
        )

    soup = BeautifulSoup(r.text, HTML_PARSER)
    cal = soup.find(attrs={"data-controller": "broneering"})
    seat.token = extract_token(r.text) or seat.token
    if not cal:
        # Not a seat-death signature (token present, but also no calendar
        # element at all) - treat as a markup-change condition instead.
        raise BookingHttpError("r17_step4_replay", "calendar element missing on replay", r.text)

    raw_dates = cal.get("data-vastuvotukuupaevad")
    dates = json.loads(raw_dates) if raw_dates else []
    seat.last_seen_dates = seat.available_dates
    seat.available_dates = dates
    return dates


def cache_applicant_on_seat(seat: Seat, applicant: dict[str, str]) -> None:
    """Stash applicant data on the Seat object so resolve_r17_refresh_dates()
    can replay step 4 later without the caller having to pass it through
    every call site. Deliberately a separate function rather than a Seat
    field so establish_seat() itself doesn't need to retain applicant data
    for longer than the one call it needs it for."""
    seat._applicant_cache = dict(applicant)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Step 5: query free times (no CAPTCHA, repeatable) - R15, R16, R18
# ---------------------------------------------------------------------------

def query_free_times(seat: Seat, date_iso: str) -> list[Slot]:
    """Queries bookable time slots for one date. No CAPTCHA required (F6).
    Callers MUST query dates sequentially (R16) - the server tracks a
    "currently selected date" in session state and concurrent queries may
    corrupt it.

    Raises SeatDeadError if the R13 death signature is detected. An empty
    slot list on a *live* seat is a completely normal result (R18) - it
    means the date genuinely has zero free times right now, not that
    anything is wrong.
    """
    d = _iso_to_ddmmyyyy(date_iso)
    r = _throttled(
        seat.session.post,
        URL,
        headers=TURBO_HEADERS,
        data={
            "broneering[kuupaev]": d,
            "broneering[markused]": "",
            "g-recaptcha-response": "",
            "broneering[__dynamic_error]": "",
            "broneering[_token]": seat.token,
            "turbo": "",
        },
        timeout=30,
    )
    r.raise_for_status()

    if is_seat_dead(r.text):
        seat.alive = False
        raise SeatDeadError(
            f"seat died while querying free times for {date_iso} after "
            f"{seat.requests_used} requests / {seat.age_seconds():.0f}s",
            html=r.text,
        )

    soup = BeautifulSoup(r.text, HTML_PARSER)
    seat.token = extract_token(r.text) or seat.token

    slots: list[Slot] = []
    for cb in soup.find_all("input", {"name": "broneering[vastuvotud][]"}):
        lab = soup.find("label", {"for": cb.get("id")}) if cb.get("id") else None
        slots.append(Slot(value=cb.get("value", ""), time=lab.get_text(strip=True) if lab else ""))
    return slots


# ---------------------------------------------------------------------------
# Step 6: submit the booking (R19-R25) - requires a CAPTCHA token
# ---------------------------------------------------------------------------

def submit_booking(
    seat: Seat,
    date_iso: str,
    slot_value: str,
    captcha_token: str,
    notes: str = "",
) -> tuple[bool, str]:
    """Submits the final booking. Returns (confirmed, response_text).

    `confirmed` is determined by the same page-text markers used throughout
    this project (spec §5 step 6): "Booked time", "Add new booking", or the
    date in DD.MM.YYYY form alongside "Delete". Per spec §8's failure-mode
    table ("Booking submitted, confirmation unclear -> Do NOT set the booked
    flag"), the caller must treat confirmed=False as "unknown outcome,
    escalate" rather than "definitely failed" - a submit that times out or
    returns something unexpected is NOT the same as a clean rejection.
    """
    d = _iso_to_ddmmyyyy(date_iso)
    r = _throttled(
        seat.session.post,
        URL,
        headers=FULL_HEADERS,
        data={
            "broneering[kuupaev]": d,
            "broneering[vastuvotud][]": slot_value,
            "broneering[markused]": notes,
            "g-recaptcha-response": captcha_token,
            "broneering[__dynamic_error]": "",
            "broneering[_token]": seat.token,
            "broneeri": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    seat.token = extract_token(r.text) or seat.token

    return is_booking_confirmed(r.text, d), r.text


def find_delete_link(html: str) -> Optional[str]:
    """The confirmation page's `<a href="/en/delete?id=...">Delete</a>`.

    Captured from a real confirmation page on 2026-08-02
    (`probe_http_flow.py` with PROBE_BOOK=1): the delete action is a plain
    link carrying a per-booking id. Its presence is the single most reliable
    signal that a booking actually exists, because the site has no reason to
    render one otherwise. Returned so the caller can store it - it is also
    the fastest way for a human to cancel a superseded appointment.
    """
    m = re.search(r'href="(/[a-z]{2}/delete\?id=[^"]+)"', html)
    return m.group(1) if m else None


def is_booking_confirmed(html: str, date_ddmmyyyy: str) -> bool:
    """Did this response confirm a booking?

    Rewritten 2026-08-03 after a real live failure. On 2026-08-03 06:05 the
    Playwright booker selected a genuine 10:15 slot, clicked submit, and then
    reported `submit_uncertain` - leaving nobody able to say whether an
    appointment existed. Two flaws caused that:

      1. The markers were pure page text ("Booked time" / "Add new booking" /
         "Delete"), which are English-locale strings on a site that also
         serves Estonian, and one of them - the date marker - was formatted
         as `2026.08.05` when the site renders `05.08.2026`, so it could
         never match at all.
      2. There was no NEGATIVE check, so "no positive marker found" was
         indistinguishable from "the page said Booking failed".

    Now: the structural delete link is the primary signal (see
    find_delete_link), an explicit failure string is a hard negative that
    overrides everything, and the text markers remain only as a secondary
    positive. Getting this wrong in the optimistic direction is far worse
    than in the pessimistic one - a false "confirmed" would stop the daemon
    hunting for a booking that does not exist - so the failure string wins
    any disagreement.
    """
    if find_delete_link(html):
        return True
    soup = BeautifulSoup(html, HTML_PARSER)
    text = soup.get_text(" ", strip=True)
    if "Booking failed" in text:
        return False
    return (
        "Booked time" in text
        or "Add new booking" in text
        or (date_ddmmyyyy in text and "Delete" in text)
    )


# ---------------------------------------------------------------------------
# Step 7: delete (test cleanup only) - R T7's "verify the delete succeeded"
# ---------------------------------------------------------------------------

def delete_booking(seat: Seat, confirmation_html: str) -> tuple[bool, str]:
    """Deletes a just-created booking from its confirmation page. Links take
    priority over forms - see the "dummy test booking left live" incident in
    AI_CONTEXT.md §10.4.2: the confirmation page's only <form> has an empty
    action and wraps the real `<a href="/en/delete?id=...">Delete</a>` link,
    so matching the form first posts a silent no-op and leaves the booking
    live. Returns (deleted, page_text_after_delete)."""
    soup = BeautifulSoup(confirmation_html, HTML_PARSER)
    links = [
        (a.get_text(strip=True), a.get("href"))
        for a in soup.find_all("a")
        if a.get_text(strip=True).lower() in ("delete", "cancel", "kustuta", "tuhista")
    ]
    if links:
        href = links[0][1]
        if href and href.startswith("/"):
            href = "https://broneering.mfa.ee" + href
        dr = _throttled(seat.session.get, href, timeout=30)
        dr.raise_for_status()
        dtext = BeautifulSoup(dr.text, HTML_PARSER).get_text(" ", strip=True)
        return "Booked time" not in dtext, dtext

    # Fallback: form-matching, only if no explicit delete/cancel link exists.
    forms = soup.find_all("form")
    target_form = None
    for f in forms:
        blob = (f.get_text(" ", strip=True) + " " + str(f.get("action") or "")).lower()
        if any(w in blob for w in ("delete", "cancel", "kustuta", "tuhista")):
            target_form = f
            break
    if target_form is None:
        return False, "no delete link or form found"

    payload: dict[str, str] = {}
    for i_ in target_form.find_all("input"):
        if i_.get("name"):
            payload[i_["name"]] = i_.get("value", "")
    for b in target_form.find_all("button"):
        if b.get("name"):
            payload[b["name"]] = b.get("value", "")
    action = target_form.get("action") or URL
    if action.startswith("/"):
        action = "https://broneering.mfa.ee" + action
    dr = _throttled(seat.session.post, action, headers=TURBO_HEADERS, data=payload, timeout=30)
    dr.raise_for_status()
    dtext = BeautifulSoup(dr.text, HTML_PARSER).get_text(" ", strip=True)
    return "Booked time" not in dtext, dtext
