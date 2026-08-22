#!/usr/bin/env python3
"""
Unit tests for the daemon's pure-logic pieces - date/time
preference rules including the multi-date fallback, plus
`estonia_booking_http`'s seat-death detection and CSRF-token extraction.

Round-2 follow-up additions: unit tests for the global rate limiter (Fix 3,
tested with a fake clock/sleep_fn - no real waiting, no network) and for the
poll/date-refresh scheduling logic (Fixes 1/2/5 - `maybe_refresh_dates()`'s
gating and suspect-detection behaviour, with `resolve_r17_refresh_dates()`
and `SeatManager.acquire()` monkeypatched so no real HTTP/CAPTCHA calls are
made).

These all run with no network access and no live credentials, which is why
they were the part of the original test plan that could be executed
directly in the implementation sandbox - other planned tests need real HTTP
access (no egress to broneering.mfa.ee, no CAPSOLVER_API_KEY/Telegram
credentials available in that session) and must be run for real against
Abu Dhabi/Cairo/Astana before trusting this in production.

Usage:
    venv/bin/python3 test_daemon_units.py
Exits 0 and prints "ALL TESTS PASSED" on success, exits 1 with a message on
the first failure.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import time as time_module
from datetime import datetime, timedelta, timezone

import estonia_booker_daemon as daemon
import estonia_booking_http as http_flow

# Redirect every production artifact path into a throwaway temp directory
# BEFORE any test runs. On 2026-08-03 a test run wrote fake BOOKING_ATTEMPT
# lines (slot_id=999, "Stored booked_date 'garbage'"), a bogus SEAT_DIED, and
# a fake daemon_dumps/*_booking_confirmed_40.html into the LIVE daemon log and
# dumps directory, while a real availability episode was being investigated.
# Test output that is indistinguishable from production output actively
# damages incident diagnosis, which is the one thing these logs exist for.
import pathlib as _pathlib
import tempfile as _tempfile

_TEST_TMP = _pathlib.Path(_tempfile.mkdtemp(prefix="daemon_unittest_"))
daemon.DAEMON_LOG_FILE = _TEST_TMP / "daemon.log"
daemon.POLLS_CSV_FILE = _TEST_TMP / "daemon_polls.csv"
daemon.DUMPS_DIR = _TEST_TMP / "daemon_dumps"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def iso(days_from_today: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days_from_today)).strftime("%Y-%m-%d")


class FakeClock:
    """A controllable clock for testing RateLimiter without any real
    waiting. `advance()` is also used as the limiter's `sleep_fn` - "sleeping"
    just moves the fake clock forward instantly."""

    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.t += seconds


def main() -> int:
    # --- rank_dates(): round-6 - EARLIEST first, today and past excluded ---
    # Replaced the original "latest date >=3 days out" rule. Earlier is now
    # strictly better because the daemon rebooks whenever an earlier date appears.
    dates = [iso(1), iso(4), iso(10), iso(2)]
    ranked = daemon.rank_dates(dates)
    check(
        "rank_dates: earliest future date ranked first (was: latest >=3 days out)",
        ranked[0] == iso(1),
        f"got {ranked}",
    )
    check(
        "rank_dates: full ascending ordering, so the poll loop walks every date nearest-first (R22)",
        ranked == [iso(1), iso(2), iso(4), iso(10)],
        f"got {ranked}",
    )
    check(
        "rank_dates: the old 3-day minimum is gone - tomorrow is acceptable",
        daemon.rank_dates([iso(1)]) == [iso(1)],
        f"got {daemon.rank_dates([iso(1)])}",
    )
    check(
        "rank_dates: TODAY is excluded (a same-day appointment is not usefully bookable)",
        daemon.rank_dates([iso(0), iso(3)]) == [iso(3)],
        f"got {daemon.rank_dates([iso(0), iso(3)])}",
    )
    check(
        "rank_dates: past dates are excluded (the calendar has been seen listing them)",
        daemon.rank_dates([iso(-5), iso(2)]) == [iso(2)],
        f"got {daemon.rank_dates([iso(-5), iso(2)])}",
    )
    check(
        "rank_dates: malformed date strings are silently skipped",
        daemon.rank_dates(["not-a-date", iso(10)]) == [iso(10)],
    )

    # --- should_book_date(): round-6 improvement gate ----------------------
    # The permanent "stop once booked" flag is gone: the daemon keeps hunting
    # and takes any STRICTLY EARLIER date, leaving the owner to delete the
    # superseded appointment.
    real_load = daemon.load_booked_state
    try:
        daemon.load_booked_state = lambda: {}
        check(
            "should_book_date: nothing booked yet -> book anything",
            daemon.should_book_date(iso(9)) is True,
        )

        daemon.load_booked_state = lambda: {"booked": True, "booked_date": iso(10)}
        check(
            "should_book_date: an EARLIER date than the one held -> rebook",
            daemon.should_book_date(iso(3)) is True,
        )
        check(
            "should_book_date: a LATER date than the one held -> do not rebook",
            daemon.should_book_date(iso(20)) is False,
        )
        check(
            "should_book_date: the SAME date as the one held -> do not rebook (no churn)",
            daemon.should_book_date(iso(10)) is False,
        )

        # Legacy state written by the old Playwright booker, before
        # booked_date existed: the date lives under booking_details.
        daemon.load_booked_state = lambda: {"booked": True, "booking_details": {"date": iso(10)}}
        check(
            "should_book_date: reads a legacy state file's booking_details.date",
            daemon.should_book_date(iso(4)) is True and daemon.should_book_date(iso(14)) is False,
        )

        daemon.load_booked_state = lambda: {"booked": True, "booked_date": "garbage"}
        with contextlib.redirect_stdout(io.StringIO()):
            unparseable = daemon.should_book_date(iso(4))
        check(
            "should_book_date: an unparseable stored date refuses to rebook rather than churning",
            unparseable is False,
        )
    finally:
        daemon.load_booked_state = real_load

    # --- Round-7: idle date-refresh backoff --------------------------------
    # A seat has a ~100-request budget. Between episodes the calendar is
    # empty, and refreshing it every 10s burns that budget in ~17 minutes -
    # so the seat is usually dead by the time the next episode starts, and we
    # pay the 20s CAPTCHA acquisition all over again. Backing off to 60s while
    # idle stretches the same budget to ~100 minutes.
    _refresh_calls: list[str] = []
    _real_resolve = daemon.resolve_r17_refresh_dates
    _real_csv = daemon.append_poll_csv
    daemon.resolve_r17_refresh_dates = lambda seat: (_refresh_calls.append("x") or seat.available_dates)
    daemon.append_poll_csv = lambda *a, **kw: None
    try:
        def _mk(dates):
            m = daemon.SeatManager()
            m.seat = http_flow.Seat(
                session=http_flow.new_session(), embassy_id="40", token="t",
                established_at=time_module.time(),
                available_dates=list(dates), last_seen_dates=list(dates),
            )
            return m

        # Empty calendar, 30s since last refresh: under the 60s idle interval
        # but over the 10s active one - must NOT refresh.
        m = _mk([])
        m.last_date_refresh_ts = time_module.time() - 30
        _refresh_calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.maybe_refresh_dates(m, forced=False, elapsed_since_trigger=None)
        check(
            "Round-7: empty calendar + 30s elapsed -> NO refresh (idle interval is 60s, not 10s)",
            _refresh_calls == [],
            f"refresh_calls={_refresh_calls}",
        )

        m.last_date_refresh_ts = time_module.time() - (daemon.IDLE_DATE_REFRESH_INTERVAL_SECONDS + 1)
        _refresh_calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.maybe_refresh_dates(m, forced=False, elapsed_since_trigger=None)
        check(
            "Round-7: empty calendar + idle interval elapsed -> refresh happens",
            len(_refresh_calls) == 1,
            f"refresh_calls={_refresh_calls}",
        )

        # Dates present: the fast 10s cadence must still apply.
        m2 = _mk([iso(5)])
        m2.last_date_refresh_ts = time_module.time() - 30
        _refresh_calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.maybe_refresh_dates(m2, forced=False, elapsed_since_trigger=None)
        check(
            "Round-7: dates present + 30s elapsed -> refresh happens (active cadence unchanged at 10s)",
            len(_refresh_calls) == 1,
            f"refresh_calls={_refresh_calls}",
        )

        # A trigger must cut through the idle backoff - this is what makes the
        # parked seat responsive the instant an episode starts.
        m3 = _mk([])
        m3.last_date_refresh_ts = time_module.time()      # just refreshed
        m3.last_forced_refresh_ts = 0.0                   # no recent forced refresh
        _refresh_calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.maybe_refresh_dates(m3, forced=True, elapsed_since_trigger=1.0)
        check(
            "Round-7: a trigger forces a refresh even while idle-backed-off (episode responsiveness preserved)",
            len(_refresh_calls) == 1,
            f"refresh_calls={_refresh_calls}",
        )
    finally:
        daemon.resolve_r17_refresh_dates = _real_resolve
        daemon.append_poll_csv = _real_csv

    # --- Round-9: slot-availability notifications are change-driven --------
    # SLOT_FOUND fires every 2s while a seat is parked (80+ times in the
    # 2026-08-03 T7 run). Telegramming each one would bury the booking
    # confirmation in noise, so notify on change only, with a floor interval.
    _sent: list[str] = []
    _real_tg = daemon.send_telegram
    daemon.send_telegram = lambda msg: _sent.append(msg)
    try:
        daemon._last_slots_signature = ""
        daemon._last_slots_notify_ts = 0.0
        daemon.notify_available_slots("2026-08-18", ["10:00", "12:15"], None)
        check(
            "Round-9: the first slot sighting sends a Telegram listing the times",
            len(_sent) == 1 and "10:00" in _sent[0] and "12:15" in _sent[0],
            f"sent={_sent}",
        )
        check(
            "Round-9: with nothing booked, the message says the earliest is being attempted",
            "Nothing booked yet" in _sent[0],
            f"sent={_sent[0][:120]}",
        )

        daemon.notify_available_slots("2026-08-18", ["10:00", "12:15"], None)
        check(
            "Round-9: an IDENTICAL slot list does not resend (this is what stops 2s spam)",
            len(_sent) == 1,
            f"sent={len(_sent)}",
        )

        # A changed list inside the floor interval is still suppressed.
        daemon.notify_available_slots("2026-08-18", ["10:00"], None)
        check(
            "Round-9: a changed list within SLOTS_NOTIFY_MIN_INTERVAL_SECONDS is held back (anti-flap)",
            len(_sent) == 1,
            f"sent={len(_sent)}",
        )

        daemon._last_slots_notify_ts = time_module.time() - (daemon.SLOTS_NOTIFY_MIN_INTERVAL_SECONDS + 1)
        daemon.notify_available_slots("2026-08-18", ["10:00"], "2026-08-11")
        check(
            "Round-9: once the interval passes, a changed list is sent",
            len(_sent) == 2,
            f"sent={len(_sent)}",
        )
        check(
            "Round-9: when a booking is already held, the message says these are only taken if EARLIER",
            "2026-08-11" in _sent[1] and "EARLIER" in _sent[1],
            f"sent={_sent[1][:160]}",
        )
    finally:
        daemon.send_telegram = _real_tg

    # --- is_booking_confirmed(): rewritten after the 2026-08-03 failure ----
    dmy = "05.08.2026"
    check(
        "is_booking_confirmed: a /en/delete?id= link is the primary positive signal",
        http_flow.is_booking_confirmed('<a href="/en/delete?id=6a6ecc0761de0">Delete</a>', dmy) is True,
    )
    check(
        "find_delete_link: returns the href so the cancel URL can be stored",
        http_flow.find_delete_link('<a href="/en/delete?id=abc123">Delete</a>') == "/en/delete?id=abc123",
    )
    check(
        "is_booking_confirmed: 'Booking failed' is a hard negative",
        http_flow.is_booking_confirmed("<p>Booking failed</p>", dmy) is False,
    )
    check(
        "is_booking_confirmed: 'Booking failed' overrides a stray text marker",
        http_flow.is_booking_confirmed("<p>Booking failed. Add new booking</p>", dmy) is False,
    )
    check(
        "is_booking_confirmed: text markers still work as a secondary positive",
        http_flow.is_booking_confirmed("<p>Booked time 10:15</p>", dmy) is True,
    )
    check(
        "is_booking_confirmed: the date marker uses DD.MM.YYYY, the format the site actually renders",
        http_flow.is_booking_confirmed(f"<p>{dmy} Delete</p>", dmy) is True,
    )
    check(
        "is_booking_confirmed: the OLD broken marker (2026.08.05) no longer produces a false positive",
        http_flow.is_booking_confirmed("<p>2026.08.05 something</p>", dmy) is False,
    )
    check(
        "is_booking_confirmed: an unrelated page is not confirmed",
        http_flow.is_booking_confirmed("<p>Choose the representation</p>", dmy) is False,
    )

    # --- choose_time(): preference for 12:00-14:00 ---
    check(
        "choose_time: prefers earliest slot in 12:00-14:00",
        daemon.choose_time(["09:00", "13:15", "12:00", "18:00"]) == "12:00",
    )
    check(
        "choose_time: falls back to earliest overall when nothing is in 12:00-14:00",
        daemon.choose_time(["09:00", "18:00", "10:30"]) == "09:00",
    )
    check(
        "choose_time: returns None for an empty list",
        daemon.choose_time([]) is None,
    )
    check(
        "choose_time: skips unparseable labels rather than crashing",
        daemon.choose_time(["garbage", "13:00"]) == "13:00",
    )

    # --- estonia_booking_http.extract_token(): CSRF token extraction ---
    html_with_token = '<html><body><input name="broneering[_token]" value="abc123"></body></html>'
    check(
        "extract_token: finds a plain <input> token",
        http_flow.extract_token(html_with_token) == "abc123",
    )
    check(
        "extract_token: returns None when absent",
        http_flow.extract_token("<html><body>no token here</body></html>") is None,
    )
    # Turbo-stream-wrapped variant
    turbo_html = (
        '<turbo-stream action="update" target="x"><template>'
        '<input name="broneering[_token]" value="xyz789">'
        '</template></turbo-stream>'
    )
    check(
        "extract_token: finds a token nested inside a turbo-stream/template",
        http_flow.extract_token(turbo_html) == "xyz789",
    )

    # --- estonia_booking_http.is_seat_dead(): positive death signature ---
    alive_html = (
        '<html><body>'
        '<input name="broneering[_token]" value="tok1">'
        '<input name="broneering[kuupaev]" value="">'
        '</body></html>'
    )
    check(
        "is_seat_dead: a response with both token AND date field is ALIVE",
        http_flow.is_seat_dead(alive_html) is False,
    )
    dead_html = (
        '<html><body>'
        '<input name="broneering[_token]" value="tok2">'
        '</body></html>'
    )
    check(
        "is_seat_dead: token present but date field gone => DEAD (R13 signature)",
        http_flow.is_seat_dead(dead_html) is True,
    )
    # A live seat mid-poll with checkboxes present but no date input rendered
    # in that particular fragment should NOT be misread as dead - the
    # checkbox presence itself is evidence of a live, working seat.
    checkbox_only_html = (
        '<html><body>'
        '<input name="broneering[_token]" value="tok3">'
        '<input type="checkbox" name="broneering[vastuvotud][]" value="111" id="s1">'
        '<label for="s1">10:00</label>'
        '</body></html>'
    )
    check(
        "is_seat_dead: checkboxes present (even without the date input) is NOT treated as dead",
        http_flow.is_seat_dead(checkbox_only_html) is False,
    )
    check(
        "is_seat_dead: no token at all is NOT the R13 signature (that's a different failure - markup change)",
        http_flow.is_seat_dead("<html><body>totally different page</body></html>") is False,
    )

    # =========================================================================
    # Round-2 follow-up Fix 3: RateLimiter (fake clock, no real waiting/network)
    # =========================================================================

    clock = FakeClock(start=0.0)
    limiter = http_flow.RateLimiter(rate_per_second=1.0, burst=3, clock=clock.now, sleep_fn=clock.advance)

    for i in range(3):
        limiter.acquire()
    check(
        "RateLimiter: burst allowance grants the first `burst` acquires with zero waiting",
        len(clock.sleep_calls) == 0 and clock.t == 0.0,
        f"sleep_calls={clock.sleep_calls} clock.t={clock.t}",
    )

    limiter.acquire()  # 4th acquire: bucket is empty (burst=3 already spent), rate=1/s
    check(
        "RateLimiter: 4th acquire (bucket exhausted) waits exactly 1/rate=1.0s and advances the clock accordingly",
        len(clock.sleep_calls) == 1 and abs(clock.sleep_calls[0] - 1.0) < 1e-6 and abs(clock.t - 1.0) < 1e-6,
        f"sleep_calls={clock.sleep_calls} clock.t={clock.t}",
    )

    # measured_rate(): 4 grants over 1.0s elapsed (first grant at t=0, last at t=1.0)
    check(
        "RateLimiter.measured_rate(): reports ~4 req in ~1s after the sequence above",
        abs(limiter.measured_rate() - 4.0) < 0.01,
        f"measured_rate={limiter.measured_rate()}",
    )

    # RATE_THROTTLED logging: at most once per 30s, even across many throttled
    # acquires within that window.
    clock2 = FakeClock(start=100.0)  # start > 30 so the very first throttle logs immediately
    limiter2 = http_flow.RateLimiter(rate_per_second=1.0, burst=1, clock=clock2.now, sleep_fn=clock2.advance)
    limiter2.acquire()  # spends the single burst token, no throttling yet
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(5):  # each of these waits ~1s (rate=1/s) -> ~5s of fake elapsed time, well under 30s
            limiter2.acquire()
    throttle_lines = [line for line in buf.getvalue().splitlines() if "RATE_THROTTLED" in line]
    check(
        "RateLimiter: RATE_THROTTLED logs exactly once for a burst of throttled acquires within one 30s log window",
        len(throttle_lines) == 1,
        f"throttle_lines={throttle_lines}",
    )

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        # Advancing the clock directly (not via acquire()) also refills the
        # bucket for however much time passed - 30s at rate=1/s refills a
        # full token, so the very next acquire() would succeed WITHOUT
        # throttling. Drain that refilled token first (silently, no log),
        # then acquire again with an empty bucket to force an actual
        # throttled wait - which is what should trip the "> 30s since last
        # log" condition.
        clock2.advance(30.0)
        limiter2.acquire()  # drains the token the 30s advance just refilled
        limiter2.acquire()  # bucket empty again -> throttles, and >=30s since last log -> logs
    throttle_lines_2 = [line for line in buf2.getvalue().splitlines() if "RATE_THROTTLED" in line]
    check(
        "RateLimiter: RATE_THROTTLED logs again once >=30s has passed since the last log",
        len(throttle_lines_2) == 1,
        f"throttle_lines_2={throttle_lines_2}",
    )

    # Never drops a request: even many sequential acquires against a slow
    # rate all eventually return (no exception, no None) - "block, don't drop".
    clock3 = FakeClock(start=0.0)
    limiter3 = http_flow.RateLimiter(rate_per_second=2.0, burst=2, clock=clock3.now, sleep_fn=clock3.advance)
    completed = 0
    for _ in range(10):
        limiter3.acquire()
        completed += 1
    check(
        "RateLimiter: 10 sequential acquires against a slow rate all complete (blocks, never drops/raises)",
        completed == 10,
        f"completed={completed}",
    )

    # =========================================================================
    # Round-2 follow-up Fixes 1/2/5: poll/date-refresh scheduling logic
    # =========================================================================
    # `resolve_r17_refresh_dates` and `SeatManager.acquire` are monkeypatched
    # so these tests make no real HTTP/CAPTCHA calls - see the module
    # docstring.

    def make_seat(dates: list[str]) -> http_flow.Seat:
        return http_flow.Seat(
            session=None,  # never touched - resolve_r17_refresh_dates is stubbed below
            embassy_id="244",
            token="tok",
            established_at=time_module.time(),
            available_dates=list(dates),
            last_seen_dates=list(dates),
        )

    # --- gating: refresh is skipped before the interval elapses ---
    refresh_calls: list[str] = []
    real_resolve = daemon.resolve_r17_refresh_dates
    daemon.resolve_r17_refresh_dates = lambda seat: (refresh_calls.append("called") or seat.available_dates)
    real_append_csv = daemon.append_poll_csv
    daemon.append_poll_csv = lambda *a, **kw: None  # silence CSV writes during this test

    try:
        seat_mgr = daemon.SeatManager()
        seat_mgr.seat = make_seat([iso(5)])
        seat_mgr.dates_at_acquisition = [iso(5)]
        seat_mgr.last_date_refresh_ts = time_module.time()  # "just refreshed"

        result = daemon.maybe_refresh_dates(seat_mgr, forced=False, elapsed_since_trigger=None)
        check(
            "maybe_refresh_dates: not forced, interval not yet elapsed -> skipped, no refresh call, returns False",
            result is False and len(refresh_calls) == 0,
            f"result={result} refresh_calls={refresh_calls}",
        )

        # --- gating: refresh DOES run once the interval has elapsed ---
        seat_mgr.last_date_refresh_ts = time_module.time() - (daemon.DATE_REFRESH_INTERVAL_SECONDS + 1)
        result = daemon.maybe_refresh_dates(seat_mgr, forced=False, elapsed_since_trigger=None)
        check(
            "maybe_refresh_dates: not forced, interval elapsed -> refresh IS called, returns False (nothing suspect for non-forced)",
            result is False and len(refresh_calls) == 1,
            f"result={result} refresh_calls={refresh_calls}",
        )

        # --- Fix 8 (round 3): forced refresh is debounced - identical or not
        # -   -no-longer-suspect logic lives in maybe_refresh_dates() at all.
        # An earlier version's "forced + identical dates -> suspect ->
        # reacquire" tests tested behaviour that was later deliberately
        # removed - replaced below with debounce tests.
        seat_mgr.seat = make_seat([iso(5)])
        seat_mgr.last_forced_refresh_ts = time_module.time()  # "just force-refreshed"
        refresh_calls.clear()
        result = daemon.maybe_refresh_dates(seat_mgr, forced=True, elapsed_since_trigger=1.0)
        check(
            "maybe_refresh_dates: forced, but within FORCED_REFRESH_MIN_INTERVAL_SECONDS of the last forced refresh -> debounced, no call",
            result is False and len(refresh_calls) == 0,
            f"result={result} refresh_calls={refresh_calls}",
        )

        seat_mgr.last_forced_refresh_ts = time_module.time() - (daemon.FORCED_REFRESH_MIN_INTERVAL_SECONDS + 1)
        result = daemon.maybe_refresh_dates(seat_mgr, forced=True, elapsed_since_trigger=1.0)
        check(
            "maybe_refresh_dates: forced, debounce interval elapsed -> refresh IS called this time",
            result is False and len(refresh_calls) == 1,
            f"result={result} refresh_calls={refresh_calls}",
        )
        check(
            "maybe_refresh_dates: forced refresh never sets last_reacquire_ts - that decision lives entirely in check_suspect_and_maybe_reacquire() now",
            seat_mgr.last_reacquire_ts == 0.0,
            f"last_reacquire_ts={seat_mgr.last_reacquire_ts}",
        )
    finally:
        daemon.resolve_r17_refresh_dates = real_resolve
        daemon.append_poll_csv = real_append_csv

    # =========================================================================
    # Round-3 Fixes 7/9/10: check_suspect_and_maybe_reacquire()
    # =========================================================================
    # `send_telegram` is left as-is (it safely no-ops with a log line when no
    # Telegram credentials are configured, which is always true in this
    # sandbox - see round 1/2's changelog entries) so these tests make no
    # network calls. `SeatManager.begin_overlapping_reacquire` is
    # monkeypatched per-test to capture calls without spawning real
    # threads/CAPTCHA solves. Time is controlled by setting
    # seat_mgr.last_trigger_ts / last_reacquire_ts to offsets from the real
    # `time.time()` (the daemon's suspect-check functions call `time.time()`
    # directly, not through an injectable clock like RateLimiter - so this is
    # "fake" in the sense of pre-computed offsets rather than real waiting,
    # not a fully injected clock; noted explicitly since round 2's RateLimiter
    # tests used a true injectable fake clock and this is a different,
    # lighter-weight technique for the same no-real-waiting goal).

    def fresh_seat_mgr(dates: list[str]) -> daemon.SeatManager:
        sm = daemon.SeatManager()
        sm.seat = make_seat(dates)
        return sm

    # --- fires at threshold, trigger fresh, cooldown long elapsed ---
    sm = fresh_seat_mgr([iso(5)])
    sm.last_trigger_ts = time_module.time() - 5  # well within TRIGGER_FRESH_WINDOW_SECONDS (60)
    sm.last_reacquire_ts = 0.0  # cooldown trivially elapsed (epoch 0)
    sm.empty_poll_streak = daemon.SUSPECT_AFTER_EMPTY_POLLS
    reacquire_calls: list[str] = []
    sm.begin_overlapping_reacquire = lambda reason: (reacquire_calls.append(reason) or True)
    daemon.check_suspect_and_maybe_reacquire(sm, elapsed_since_trigger=5.0)
    check(
        "check_suspect_and_maybe_reacquire: streak at threshold + fresh trigger + cooldown elapsed -> fires, resets streak, records last_reacquire_ts",
        reacquire_calls == ["suspect"] and sm.empty_poll_streak == 0 and sm.last_reacquire_ts > 0,
        f"reacquire_calls={reacquire_calls} empty_poll_streak={sm.empty_poll_streak} last_reacquire_ts={sm.last_reacquire_ts}",
    )

    # --- below threshold: no action ---
    sm2 = fresh_seat_mgr([iso(5)])
    sm2.last_trigger_ts = time_module.time() - 5
    sm2.last_reacquire_ts = 0.0
    sm2.empty_poll_streak = daemon.SUSPECT_AFTER_EMPTY_POLLS - 1
    reacquire_calls2: list[str] = []
    sm2.begin_overlapping_reacquire = lambda reason: (reacquire_calls2.append(reason) or True)
    daemon.check_suspect_and_maybe_reacquire(sm2, elapsed_since_trigger=5.0)
    check(
        "check_suspect_and_maybe_reacquire: streak below threshold -> no action, streak untouched",
        reacquire_calls2 == [] and sm2.empty_poll_streak == daemon.SUSPECT_AFTER_EMPTY_POLLS - 1,
        f"reacquire_calls2={reacquire_calls2} empty_poll_streak={sm2.empty_poll_streak}",
    )

    # --- stale trigger: streak at/above threshold but trigger too old -> ignored ---
    sm3 = fresh_seat_mgr([iso(5)])
    sm3.last_trigger_ts = time_module.time() - (daemon.TRIGGER_FRESH_WINDOW_SECONDS + 5)  # stale
    sm3.last_reacquire_ts = 0.0
    sm3.empty_poll_streak = daemon.SUSPECT_AFTER_EMPTY_POLLS + 3  # well above threshold
    reacquire_calls3: list[str] = []
    sm3.begin_overlapping_reacquire = lambda reason: (reacquire_calls3.append(reason) or True)
    daemon.check_suspect_and_maybe_reacquire(sm3, elapsed_since_trigger=None)
    check(
        "check_suspect_and_maybe_reacquire: stale trigger -> ignored even with a high empty-poll streak",
        reacquire_calls3 == [] and sm3.empty_poll_streak == daemon.SUSPECT_AFTER_EMPTY_POLLS + 3,
        f"reacquire_calls3={reacquire_calls3} empty_poll_streak={sm3.empty_poll_streak}",
    )

    # --- reset on non-empty poll: simulated the way poll_cycle() does it ---
    sm4 = fresh_seat_mgr([iso(5)])
    sm4.empty_poll_streak = daemon.SUSPECT_AFTER_EMPTY_POLLS - 1  # one empty poll away from firing
    sm4.empty_poll_streak = 0  # poll_cycle() does exactly this the instant any date returns a non-empty slot list
    check(
        "SeatManager.empty_poll_streak: a non-empty poll result resets the streak to 0 (poll_cycle()'s behaviour, exercised directly here)",
        sm4.empty_poll_streak == 0,
    )

    # --- Fix 10: cooldown suppression ---
    sm5 = fresh_seat_mgr([iso(5)])
    sm5.last_trigger_ts = time_module.time() - 5
    sm5.last_reacquire_ts = time_module.time() - 10  # fired 10s ago, well inside the 300s cooldown
    sm5.empty_poll_streak = daemon.SUSPECT_AFTER_EMPTY_POLLS
    reacquire_calls5: list[str] = []
    sm5.begin_overlapping_reacquire = lambda reason: (reacquire_calls5.append(reason) or True)
    last_reacquire_before = sm5.last_reacquire_ts
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        daemon.check_suspect_and_maybe_reacquire(sm5, elapsed_since_trigger=5.0)
    suppressed_lines = [line for line in buf3.getvalue().splitlines() if "REACQUIRE_SUPPRESSED" in line]
    check(
        "check_suspect_and_maybe_reacquire: within REACQUIRE_COOLDOWN_SECONDS -> suppressed, no reacquire, streak reset, last_reacquire_ts untouched",
        reacquire_calls5 == []
        and sm5.empty_poll_streak == 0
        and sm5.last_reacquire_ts == last_reacquire_before
        and len(suppressed_lines) == 1,
        f"reacquire_calls5={reacquire_calls5} empty_poll_streak={sm5.empty_poll_streak} "
        f"last_reacquire_ts_changed={sm5.last_reacquire_ts != last_reacquire_before} suppressed_lines={suppressed_lines}",
    )

    # --- Fix 9: reacquisition never drops the seat first ---
    sm6 = fresh_seat_mgr([iso(5)])
    sm6.last_trigger_ts = time_module.time() - 5
    sm6.last_reacquire_ts = 0.0
    sm6.empty_poll_streak = daemon.SUSPECT_AFTER_EMPTY_POLLS
    seat_was_live_at_call_time = []

    def _capture_seat_state(reason):
        seat_was_live_at_call_time.append(sm6.seat is not None and sm6.has_live_seat())
        return True

    sm6.begin_overlapping_reacquire = _capture_seat_state
    daemon.check_suspect_and_maybe_reacquire(sm6, elapsed_since_trigger=5.0)
    check(
        "check_suspect_and_maybe_reacquire: the seat is STILL live at the moment begin_overlapping_reacquire() is called - never dropped first",
        seat_was_live_at_call_time == [True] and sm6.seat is not None,
        f"seat_was_live_at_call_time={seat_was_live_at_call_time} sm6.seat={sm6.seat}",
    )

    # --- SeatManager.begin_overlapping_reacquire itself: reuses the SAME
    # renewal machinery/lock for both reasons (only one allowed in flight) ---
    sm7 = fresh_seat_mgr([iso(5)])
    with sm7._renewal_lock:
        sm7._renewal_in_progress = True  # simulate a renewal already in flight
    started = sm7.begin_overlapping_reacquire(reason="suspect")
    check(
        "begin_overlapping_reacquire: refuses to start a second reacquisition while one is already in flight (R14, shared with TTL renewal)",
        started is False,
        f"started={started}",
    )
    with sm7._renewal_lock:
        sm7._renewal_in_progress = False

    # --- Fix 1: SeatManager scheduling state resets force an immediate first poll ---
    seat_mgr2 = daemon.SeatManager()
    check(
        "SeatManager: starts with last_slot_poll_ts=0.0 (so the very first main-loop tick after acquire polls immediately)",
        seat_mgr2.last_slot_poll_ts == 0.0,
    )
    seat_mgr2.last_slot_poll_ts = 12345.0
    seat_mgr2.empty_poll_streak = 7
    seat_mgr2.drop_seat()
    check(
        "SeatManager.drop_seat(): resets last_slot_poll_ts/last_date_refresh_ts/dates_at_acquisition/empty_poll_streak",
        seat_mgr2.last_slot_poll_ts == 0.0
        and seat_mgr2.last_date_refresh_ts == 0.0
        and seat_mgr2.dates_at_acquisition == []
        and seat_mgr2.empty_poll_streak == 0,
    )

    # =========================================================================
    # Round-4 Fix 11: reserve prewarm runs CONCURRENTLY with acquisition's own
    # CAPTCHA solve, not after establish_seat() returns. `solve_capsolver`
    # (the reserve's solve) and `solve_on_demand` (the acquisition's own
    # solve) are both monkeypatched to block on a shared release gate until
    # BOTH have signalled they've started - proving neither waited for the
    # other before beginning.
    # =========================================================================

    reserve_solve_started = threading.Event()
    acquisition_solve_started = threading.Event()
    release_solves = threading.Event()

    def fake_solve_capsolver_concurrent(timeout=120):
        reserve_solve_started.set()
        release_solves.wait(3)
        return "RESERVE_TOKEN"

    def fake_solve_on_demand_concurrent(timeout=120):
        acquisition_solve_started.set()
        release_solves.wait(3)
        return "ACQUISITION_TOKEN"

    def fake_check_service_offered(session, embassy_id):
        return True, ["Long-stay visa (D-visa) application"], "<html></html>"

    def fake_establish_seat(embassy_id, applicant, token, session=None):
        return make_seat([iso(5)])

    real_solve_capsolver = daemon.solve_capsolver
    real_solve_on_demand = daemon.solve_on_demand
    real_check_service_offered = daemon.check_service_offered
    real_establish_seat = daemon.establish_seat
    real_load_applicant_data = daemon.load_applicant_data
    real_cache_applicant = daemon.cache_applicant_on_seat
    real_should_book = daemon.should_book_date
    real_mark_booked = daemon.mark_booked
    real_send_telegram = daemon.send_telegram
    real_submit_booking = daemon.submit_booking

    daemon.solve_capsolver = fake_solve_capsolver_concurrent
    daemon.solve_on_demand = fake_solve_on_demand_concurrent
    daemon.check_service_offered = fake_check_service_offered
    daemon.establish_seat = fake_establish_seat
    daemon.load_applicant_data = lambda: {"specifying_information": "x"}
    daemon.cache_applicant_on_seat = lambda seat, applicant: None

    seat_mgr_11 = daemon.SeatManager()
    try:
        acquire_thread = threading.Thread(target=seat_mgr_11.acquire, kwargs={"elapsed_since_trigger": 0.0})
        acquire_thread.start()
        reserve_ok = reserve_solve_started.wait(2)
        acquisition_ok = acquisition_solve_started.wait(2)
        check(
            "Fix 11: reserve's prewarm solve starts without waiting for the acquisition's own solve to finish first",
            reserve_ok,
            f"reserve_solve_started={reserve_solve_started.is_set()}",
        )
        check(
            "Fix 11: acquisition's own CAPTCHA solve starts without waiting for the reserve's prewarm solve to finish first",
            acquisition_ok,
            f"acquisition_solve_started={acquisition_solve_started.is_set()}",
        )
        release_solves.set()
        acquire_thread.join(timeout=3)
        check(
            "Fix 11: acquire() completes successfully once both concurrent solves are released",
            not acquire_thread.is_alive() and seat_mgr_11.has_live_seat(),
            f"thread_alive={acquire_thread.is_alive()} has_live_seat={seat_mgr_11.has_live_seat()}",
        )
    finally:
        daemon.solve_capsolver = real_solve_capsolver
        daemon.solve_on_demand = real_solve_on_demand
        daemon.check_service_offered = real_check_service_offered
        daemon.establish_seat = real_establish_seat
        daemon.load_applicant_data = real_load_applicant_data
        daemon.cache_applicant_on_seat = real_cache_applicant
        seat_mgr_11.token_reserve.stop()

    # --- Fix 11 also applies to begin_overlapping_reacquire() - a
    # swapped-in seat should arrive with a warm reserve too ---
    prewarm_calls: list[str] = []
    seat_mgr_11b = fresh_seat_mgr([iso(5)])
    seat_mgr_11b.token_reserve.prewarm = lambda: prewarm_calls.append("called")
    daemon.check_service_offered = fake_check_service_offered
    daemon.solve_on_demand = lambda timeout=120: "TOKEN"
    daemon.establish_seat = fake_establish_seat
    daemon.load_applicant_data = lambda: {"specifying_information": "x"}
    daemon.cache_applicant_on_seat = lambda seat, applicant: None
    try:
        started = seat_mgr_11b.begin_overlapping_reacquire(reason="ttl_renewal")
        deadline = time_module.time() + 2
        while not prewarm_calls and time_module.time() < deadline:
            time_module.sleep(0.05)
        check(
            "Fix 11: begin_overlapping_reacquire() also prewarms the reserve for the seat it's about to swap in",
            started and prewarm_calls == ["called"],
            f"started={started} prewarm_calls={prewarm_calls}",
        )
    finally:
        daemon.check_service_offered = real_check_service_offered
        daemon.solve_on_demand = real_solve_on_demand
        daemon.establish_seat = real_establish_seat
        daemon.load_applicant_data = real_load_applicant_data
        daemon.cache_applicant_on_seat = real_cache_applicant

    # =========================================================================
    # Round-8: TokenReserve is now a continuously-maintained POOL, not a
    # single token tied to seat lifetime. The pool is what makes seat
    # acquisition instant - acquisition needs a token ~1.5s into the flow,
    # far too early to prewarm reactively, which is why every New Delhi
    # episode was previously observed ~20s late.
    # =========================================================================

    solve_call_log: list[float] = []

    _solve_lock = threading.Lock()

    def fake_solve_capsolver_log(timeout=120):
        # Thread-safe: _top_up() solves in parallel, and a naive
        # `len(solve_call_log)` read outside a lock let two concurrent solves
        # return the SAME token string - which then failed the
        # two-consecutive-takes assertion for a reason that had nothing to do
        # with the pool. Real CapSolver returns distinct tokens.
        with _solve_lock:
            solve_call_log.append(time_module.time())
            return f"TOKEN_{len(solve_call_log)}"

    daemon.solve_capsolver = fake_solve_capsolver_log
    reserve = daemon.TokenReserve()
    try:
        check(
            "Round-8: TOKEN_RESERVE_SIZE defaults to >=2 (one for acquisition, one for the submit ~2s later)",
            daemon.TOKEN_RESERVE_SIZE >= 2,
            f"TOKEN_RESERVE_SIZE={daemon.TOKEN_RESERVE_SIZE}",
        )

        # An empty pool tops up to the target, solving in parallel.
        solve_call_log.clear()
        reserve._top_up(reason="test")
        _dl = time_module.time() + 5
        while reserve.depth() < daemon.TOKEN_RESERVE_SIZE and time_module.time() < _dl:
            time_module.sleep(0.02)
        check(
            "Round-8: an empty pool fills itself to TOKEN_RESERVE_SIZE",
            reserve.depth() == daemon.TOKEN_RESERVE_SIZE,
            f"depth={reserve.depth()} solves={len(solve_call_log)}",
        )

        # A full pool must not keep spending money. Wait for every in-flight
        # solve to finish first, otherwise a straggler appending after the
        # clear looks like a spurious solve.
        _dl = time_module.time() + 5
        while reserve._solves_in_flight and time_module.time() < _dl:
            time_module.sleep(0.02)
        solve_call_log.clear()
        reserve._top_up(reason="test-nop")
        time_module.sleep(0.2)
        check(
            "Round-8: a full pool triggers no further solves (the deficit calculation counts in-flight work)",
            len(solve_call_log) == 0,
            f"solves={len(solve_call_log)}",
        )

        # take() returns a token and refills.
        before = reserve.depth()
        with contextlib.redirect_stdout(io.StringIO()):
            got = reserve.take()
        check(
            "Round-8: take() returns a pre-solved token immediately - this is the 20s -> instant win",
            isinstance(got, str) and got.startswith("TOKEN_"),
            f"got={got!r}",
        )
        _dl = time_module.time() + 5
        while reserve.depth() < before and time_module.time() < _dl:
            time_module.sleep(0.02)
        check(
            "Round-8: the pool refills after a take()",
            reserve.depth() == before,
            f"depth={reserve.depth()} expected={before}",
        )

        # Two takes in a row must both succeed - the acquire-then-submit
        # sequence is precisely what a pool of 1 would fail.
        #
        # Seed the pool directly rather than relying on the background
        # refills to have landed. Depending on concurrent solves made this
        # assertion fail intermittently for reasons unrelated to what it
        # tests (whether two takes return two DISTINCT tokens), and a test
        # that fails at random teaches you to ignore it.
        _now = time_module.time()
        with reserve._lock:
            reserve._tokens = [("POOL_A", _now - 2), ("POOL_B", _now - 1)]
        with contextlib.redirect_stdout(io.StringIO()):
            t1 = reserve.take()
            t2 = reserve.take()
        check(
            "Round-8: two consecutive takes both return tokens (acquire + submit, the sequence that wins a slot)",
            isinstance(t1, str) and isinstance(t2, str) and t1 != t2,
            f"t1={t1!r} t2={t2!r}",
        )

        # Expired tokens are pruned rather than handed out.
        with reserve._lock:
            reserve._tokens = [("ANCIENT", time_module.time() - (daemon.TOKEN_MAX_AGE_SECONDS + 10))]
        with contextlib.redirect_stdout(io.StringIO()):
            stale = reserve.take()
        check(
            "Round-8: a token older than TOKEN_MAX_AGE_SECONDS is pruned, never returned (R29)",
            stale is None,
            f"stale={stale!r}",
        )

        # The freshest token is handed out first, to maximise remaining validity.
        now = time_module.time()
        with reserve._lock:
            reserve._tokens = [("OLDER", now - 50), ("NEWER", now - 1)]
        with contextlib.redirect_stdout(io.StringIO()):
            picked = reserve.take()
        check(
            "Round-8: take() hands out the FRESHEST token, maximising validity for the imminent submit",
            picked == "NEWER",
            f"picked={picked!r}",
        )

        # --- Round-11: proactive replacement, so the pool never dips -------
        # Replacement takes 15-25s. A purely reactive pool (prune at
        # TOKEN_MAX_AGE_SECONDS, then solve) is short for that whole window.
        check(
            "Round-11: replacement age is strictly below the usable age, so a replacement "
            "starts while the old token is still valid",
            daemon.TOKEN_REFRESH_SECONDS < daemon.TOKEN_MAX_AGE_SECONDS,
            f"refresh={daemon.TOKEN_REFRESH_SECONDS} max_age={daemon.TOKEN_MAX_AGE_SECONDS}",
        )
        check(
            "Round-11/12: the gap between them exceeds a worst-case 25s solve, so the new token "
            "lands before the old one expires",
            (daemon.TOKEN_MAX_AGE_SECONDS - daemon.TOKEN_REFRESH_SECONDS) >= 25,
            f"gap={daemon.TOKEN_MAX_AGE_SECONDS - daemon.TOKEN_REFRESH_SECONDS}s",
        )
        check(
            "Round-12: a token is still discarded well inside reCAPTCHA's ~120s validity, "
            "leaving margin for the 1-2s between take() and use",
            daemon.TOKEN_MAX_AGE_SECONDS <= 112,
            f"max_age={daemon.TOKEN_MAX_AGE_SECONDS}",
        )

        _now = time_module.time()
        # Two tokens past the refresh age but still usable: the pool should
        # want two replacements while continuing to hand these out.
        with reserve._lock:
            reserve._tokens = [
                ("AGING_A", _now - (daemon.TOKEN_REFRESH_SECONDS + 5)),
                ("AGING_B", _now - (daemon.TOKEN_REFRESH_SECONDS + 5)),
            ]
            reserve._solves_in_flight = 0
        check(
            "Round-11: tokens past the refresh age no longer count toward the target, so "
            "replacements are requested",
            reserve._reserve_slots() == daemon.TOKEN_RESERVE_SIZE,
            f"reserved={reserve._reserve_slots()}",
        )
        with reserve._lock:
            reserve._solves_in_flight = 0
        with contextlib.redirect_stdout(io.StringIO()):
            still_usable = reserve.take()
        check(
            "Round-11: an aging-but-unexpired token is STILL handed out (usable != counted fresh)",
            still_usable in ("AGING_A", "AGING_B"),
            f"got={still_usable!r}",
        )

        # Fresh tokens must not trigger pointless replacement.
        with reserve._lock:
            reserve._tokens = [("FRESH_A", _now - 1), ("FRESH_B", _now - 2)]
            reserve._solves_in_flight = 0
        check(
            "Round-11: fresh tokens request no replacement (no runaway spend)",
            reserve._reserve_slots() == 0,
        )
        with reserve._lock:
            reserve._solves_in_flight = 0

        check(
            "Round-8: MAX_SOLVES_PER_HOUR was raised above the pool's own steady-state cost (~60/hr), "
            "so the safety guard cannot silently throttle the reserve",
            daemon.MAX_SOLVES_PER_HOUR >= 120,
            f"MAX_SOLVES_PER_HOUR={daemon.MAX_SOLVES_PER_HOUR}",
        )
    finally:
        daemon.solve_capsolver = real_solve_capsolver
        reserve.stop()

    # =========================================================================
    # Round-4 Fix 13: an on-demand solve at booking time must never block the
    # main loop / poll cadence, and a booking attempt must remain
    # single-flight (no concurrent submits on the same session).
    # =========================================================================

    solve_started = threading.Event()
    release_solve = threading.Event()

    def slow_solve_on_demand(timeout=120):
        solve_started.set()
        release_solve.wait(3)
        return "ON_DEMAND_TOKEN"

    submit_calls: list[tuple] = []

    def fake_submit_booking(seat, date_iso, slot_value, captcha_token, notes=""):
        submit_calls.append((date_iso, slot_value, captcha_token))
        return True, "<html>Booked time</html>"

    daemon.solve_on_demand = slow_solve_on_demand
    daemon.submit_booking = fake_submit_booking
    daemon.should_book_date = lambda d: True
    daemon.mark_booked = lambda details: None
    daemon.send_telegram = lambda msg: None
    daemon.load_applicant_data = lambda: {"specifying_information": "x"}

    seat_mgr_13 = fresh_seat_mgr([iso(5)])
    slot_13 = daemon.Slot(value="999", time="13:00")

    try:
        call_started = time_module.time()
        result = daemon.attempt_booking(seat_mgr_13, iso(5), slot_13, elapsed_since_trigger=1.0)
        call_duration = time_module.time() - call_started
        check(
            "Fix 13: attempt_booking() returns immediately when the reserve is empty, instead of blocking on the on-demand solve",
            result is False and call_duration < 1.0,
            f"result={result} call_duration={call_duration:.2f}s",
        )
        check(
            "Fix 13: the on-demand solve IS running in the background at this point",
            solve_started.is_set(),
        )
        check(
            "Fix 13: booking_in_progress is set while the background attempt is in flight",
            seat_mgr_13.booking_in_progress is True,
        )

        # A second slot found while the first is still solving must NOT
        # dispatch a second, concurrent booking attempt.
        result2 = daemon.attempt_booking(seat_mgr_13, iso(6), slot_13, elapsed_since_trigger=1.0)
        check(
            "Fix 13: a second attempt_booking() call while one is already in flight does not start another (single booking attempt at a time)",
            result2 is False and len(submit_calls) == 0,
            f"result2={result2} submit_calls={submit_calls}",
        )

        release_solve.set()
        deadline = time_module.time() + 3
        while not submit_calls and time_module.time() < deadline:
            time_module.sleep(0.05)
        check(
            "Fix 13: the background attempt eventually solves and submits (R23 preserved - just not blocking the caller)",
            len(submit_calls) == 1 and submit_calls[0][0] == iso(5),
            f"submit_calls={submit_calls}",
        )
        deadline2 = time_module.time() + 2
        while seat_mgr_13.booking_in_progress and time_module.time() < deadline2:
            time_module.sleep(0.05)
        check(
            "Fix 13: booking_in_progress is cleared once the background attempt finishes",
            seat_mgr_13.booking_in_progress is False,
        )
    finally:
        daemon.solve_on_demand = real_solve_on_demand
        daemon.submit_booking = real_submit_booking
        daemon.should_book_date = real_should_book
        daemon.mark_booked = real_mark_booked
        daemon.send_telegram = real_send_telegram
        daemon.load_applicant_data = real_load_applicant_data

    # =========================================================================
    # Round-4 Fix 14: DAEMON_DRY_RUN=1 attempts a booking only ONCE, not on
    # every poll cycle forever.
    # =========================================================================

    daemon._dry_run_completed.clear()
    real_dry_run = daemon.DAEMON_DRY_RUN
    daemon.DAEMON_DRY_RUN = True
    daemon.should_book_date = lambda d: True
    daemon.solve_on_demand = lambda timeout=120: "FAKE_DRY_RUN_TOKEN"
    telegram_msgs: list[str] = []
    daemon.send_telegram = lambda msg: telegram_msgs.append(msg)
    seat_mgr_14 = fresh_seat_mgr([iso(5)])
    slot_14 = daemon.Slot(value="111", time="12:15")

    try:
        daemon.attempt_booking(seat_mgr_14, iso(5), slot_14, elapsed_since_trigger=1.0)
        deadline = time_module.time() + 2
        while not daemon._dry_run_completed.is_set() and time_module.time() < deadline:
            time_module.sleep(0.05)
        check(
            "Fix 14: first DAEMON_DRY_RUN=1 attempt runs the full flow, logs 'would have booked', and sets the one-shot completed flag",
            daemon._dry_run_completed.is_set() and any("DRY RUN" in m for m in telegram_msgs),
            f"dry_run_completed={daemon._dry_run_completed.is_set()} telegram_msgs={telegram_msgs}",
        )
        telegram_count_after_first = len(telegram_msgs)

        result_second = daemon.attempt_booking(seat_mgr_14, iso(5), slot_14, elapsed_since_trigger=1.0)
        time_module.sleep(0.2)  # give any (unexpected) background thread a chance to misbehave
        check(
            "Fix 14: a second DAEMON_DRY_RUN=1 attempt short-circuits immediately - no further token spend, no further Telegram",
            result_second is False and len(telegram_msgs) == telegram_count_after_first,
            f"result_second={result_second} telegram_msgs={telegram_msgs}",
        )
    finally:
        daemon.DAEMON_DRY_RUN = real_dry_run
        daemon.should_book_date = real_should_book
        daemon.solve_on_demand = real_solve_on_demand
        daemon.send_telegram = real_send_telegram
        daemon._dry_run_completed.clear()

    # =========================================================================
    # Round-4 Fix 15 (minor): episode-based elapsed_since_trigger
    # (update_episode_start()) - pure function, no monkeypatching needed.
    # =========================================================================

    t0 = 1_000_000.0  # arbitrary fixed reference point - avoids any real-clock flakiness
    check(
        "update_episode_start: the very first trigger ever (previous_last_trigger_ts<=0) starts a new episode",
        daemon.update_episode_start(None, 0.0, t0) == t0,
    )
    check(
        "update_episode_start: a re-announcement within TRIGGER_FRESH_WINDOW_SECONDS of the previous one continues the SAME episode",
        daemon.update_episode_start(t0, t0, t0 + 10) == t0,
    )
    check(
        "update_episode_start: many re-announcements in a row, all within the fresh window, all still point back to the ORIGINAL episode start",
        daemon.update_episode_start(t0, t0 + 10, t0 + 20) == t0,
    )
    gap_new_trigger = t0 + 500
    check(
        "update_episode_start: a gap LONGER than TRIGGER_FRESH_WINDOW_SECONDS since the last trigger starts a NEW episode",
        daemon.update_episode_start(t0, t0 + 20, gap_new_trigger) == gap_new_trigger,
        f"got {daemon.update_episode_start(t0, t0 + 20, gap_new_trigger)}",
    )
    check(
        "update_episode_start: episode_start_ts=None always resets to the new trigger, regardless of the gap",
        daemon.update_episode_start(None, t0, t0 + 1) == t0 + 1,
    )

    # =========================================================================
    # Round-5 Fixes 16-18: request-budget renewal, death recovery, death dump
    # =========================================================================
    #
    # Context: spec F5 claimed a seat lasts 1440s on an absolute timer. Three
    # live runs at three different poll cadences instead all died at ~95-100
    # REQUESTS (~98 at 1442s polling every 15s; ~100 at 190s polling every 2s,
    # five times running; ~95 at 138s with date refreshes on top). The 1440s
    # figure was an artifact of the original measurement's interval. These
    # tests pin the corrected behaviour.

    # --- Fix 16: _throttled() counts requests onto the session --------------
    sess = http_flow.new_session()
    check(
        "Fix 16: a fresh session starts with no request_count attribute (reads as 0)",
        getattr(sess, "request_count", 0) == 0,
    )
    # `sess.close` is a real bound method of requests.Session that makes no
    # network call - exactly what's needed to exercise _throttled's counting
    # without touching the live site.
    for _ in range(3):
        http_flow._throttled(sess.close)
    check(
        "Fix 16: _throttled() increments the counter on the owning session",
        sess.request_count == 3,
        f"got {getattr(sess, 'request_count', None)}",
    )

    counted_seat = http_flow.Seat(
        session=sess, embassy_id="244", token="tok",
        established_at=time_module.time(), available_dates=[], last_seen_dates=[],
    )
    check(
        "Fix 16: Seat.requests_used reads straight off its session (cannot drift)",
        counted_seat.requests_used == 3,
        f"got {counted_seat.requests_used}",
    )

    # --- Fix 16: renewal fires on whichever threshold binds first -----------
    renew_calls: list[str] = []
    real_begin_overlap = daemon.SeatManager.begin_overlapping_reacquire
    daemon.SeatManager.begin_overlapping_reacquire = (
        lambda self, reason: (renew_calls.append(reason) or True)
    )
    try:
        def seat_with(used: int, age: float) -> http_flow.Seat:
            s = http_flow.new_session()
            s.request_count = used
            return http_flow.Seat(
                session=s, embassy_id="244", token="tok",
                established_at=time_module.time() - age,
                available_dates=["2026-08-18"], last_seen_dates=["2026-08-18"],
            )

        mgr = daemon.SeatManager()

        renew_calls.clear()
        mgr.seat = seat_with(used=daemon.SEAT_RENEW_AFTER_REQUESTS - 1, age=1.0)
        mgr.maybe_renew()
        check(
            "Fix 16: below BOTH thresholds -> no renewal",
            renew_calls == [],
            f"got {renew_calls}",
        )

        renew_calls.clear()
        mgr.seat = seat_with(used=daemon.SEAT_RENEW_AFTER_REQUESTS, age=1.0)
        mgr.maybe_renew()
        check(
            "Fix 16: request budget reached while still young -> renews (the case time-only missed entirely)",
            len(renew_calls) == 1 and "request_budget" in renew_calls[0],
            f"got {renew_calls}",
        )

        renew_calls.clear()
        mgr.seat = seat_with(used=1, age=daemon.SEAT_RENEW_AFTER_SECONDS + 1)
        mgr.maybe_renew()
        check(
            "Fix 16: time threshold still honoured when request count is low (belt-and-braces kept deliberately)",
            len(renew_calls) == 1 and "ttl_renewal" in renew_calls[0],
            f"got {renew_calls}",
        )

        renew_calls.clear()
        mgr.seat = None
        mgr.maybe_renew()
        check(
            "Fix 16: no live seat -> maybe_renew is a no-op",
            renew_calls == [],
            f"got {renew_calls}",
        )
    finally:
        daemon.SeatManager.begin_overlapping_reacquire = real_begin_overlap

    # --- Fixes 17/18: seat death dumps HTML and starts a replacement --------
    acquire_calls: list[str] = []
    dumped: list[str] = []
    real_begin_bg = daemon.SeatManager.begin_background_acquire
    real_dump = daemon.dump_html
    real_booked = daemon.should_book_date
    daemon.SeatManager.begin_background_acquire = (
        lambda self, reason: (acquire_calls.append(reason) or True)
    )
    daemon.dump_html = lambda name, html: (dumped.append((name, html)) or "/tmp/fake_dump.html")
    daemon.should_book_date = lambda d: True
    try:
        mgr = daemon.SeatManager()
        s = http_flow.new_session()
        s.request_count = 97
        mgr.seat = http_flow.Seat(
            session=s, embassy_id="244", token="tok",
            established_at=time_module.time() - 190,
            available_dates=["2026-08-18"], last_seen_dates=["2026-08-18"],
        )
        exc = http_flow.SeatDeadError("dead", html="<html>server said something</html>")
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.handle_seat_death(mgr, exc, "detected while querying 2026-08-18", 5.0)

        check(
            "Fix 18: seat death writes the response body to a dump (death has only ever been inferred until now)",
            len(dumped) == 1 and dumped[0][0] == "seat_died"
            and "server said something" in dumped[0][1],
            f"got {dumped}",
        )
        check(
            "Fix 17: seat death drops the seat AND immediately starts a replacement (no waiting for the next trigger)",
            mgr.seat is None and acquire_calls == ["seat_died"],
            f"seat={mgr.seat} acquire_calls={acquire_calls}",
        )

        # Round-6 changed this deliberately: holding a booking no longer
        # stops the hunt, so a dying seat is ALWAYS replaced. Previously this
        # asserted the opposite (no replacement once booked), which would now
        # mean the daemon stopped looking for an earlier date the moment the
        # first booking landed - exactly what the owner asked us not to do.
        acquire_calls.clear()
        daemon.should_book_date = lambda d: False  # i.e. we already hold something better
        mgr.seat = http_flow.Seat(
            session=http_flow.new_session(), embassy_id="244", token="tok",
            established_at=time_module.time(), available_dates=[], last_seen_dates=[],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.handle_seat_death(mgr, exc, "whatever", None)
        check(
            "Round-6: a dying seat is replaced even while holding a booking (the hunt never stops)",
            mgr.seat is None and acquire_calls == ["seat_died"],
            f"acquire_calls={acquire_calls}",
        )
    finally:
        daemon.SeatManager.begin_background_acquire = real_begin_bg
        daemon.dump_html = real_dump
        daemon.should_book_date = real_booked

    # --- Fix 17: the short death cooldown stops a tight spin ----------------
    spawned: list[str] = []
    real_acquire = daemon.SeatManager.acquire
    daemon.SeatManager.acquire = lambda self, elapsed_since_trigger=None: (spawned.append("acq") or True)
    try:
        mgr = daemon.SeatManager()
        with contextlib.redirect_stdout(io.StringIO()):
            first = mgr.begin_background_acquire(reason="seat_died")
            # Let the spawned thread finish so _renewal_in_progress clears and
            # the SECOND call is gated by the cooldown, not by the concurrency
            # guard - otherwise this would pass for the wrong reason.
            time_module.sleep(0.25)
            second = mgr.begin_background_acquire(reason="seat_died")
        check(
            "Fix 17: first death-recovery acquire starts, second within the cooldown is suppressed",
            first is True and second is False,
            f"first={first} second={second}",
        )
        mgr.last_death_reacquire_ts = time_module.time() - (
            daemon.SEAT_DEATH_REACQUIRE_COOLDOWN_SECONDS + 1
        )
        with contextlib.redirect_stdout(io.StringIO()):
            third = mgr.begin_background_acquire(reason="seat_died")
            time_module.sleep(0.25)
        check(
            "Fix 17: once the cooldown elapses, death recovery is allowed again",
            third is True,
            f"third={third}",
        )
    finally:
        daemon.SeatManager.acquire = real_acquire

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
