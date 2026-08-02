#!/usr/bin/env python3
"""
T1 (spec §9): unit tests for the daemon's pure-logic pieces - date/time
preference rules including the R22 multi-date fallback, plus
`estonia_booking_http`'s seat-death detection and CSRF-token extraction.

Round-2 follow-up additions: unit tests for the global rate limiter (Fix 3,
tested with a fake clock/sleep_fn - no real waiting, no network) and for the
poll/date-refresh scheduling logic (Fixes 1/2/5 - `maybe_refresh_dates()`'s
gating and suspect-detection behaviour, with `resolve_r17_refresh_dates()`
and `SeatManager.acquire()` monkeypatched so no real HTTP/CAPTCHA calls are
made).

These all run with no network access and no live credentials, which is why
they are the one item in the spec's test plan (T1-T9) that could be executed
directly in the implementation sandbox - see IMPLEMENTATION_CHANGELOG.md §5/§8
for why T2-T9 could not be run here (no egress to broneering.mfa.ee, no
CAPSOLVER_API_KEY/Telegram credentials available in this session) and must be
run for real against Abu Dhabi/Cairo/Astana before trusting this in
production.

Usage:
    venv/bin/python3 test_daemon_units.py
Exits 0 and prints "ALL TESTS PASSED" on success, exits 1 with a message on
the first failure.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time as time_module
from datetime import datetime, timedelta, timezone

import estonia_booker_daemon as daemon
import estonia_booking_http as http_flow

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
    # --- rank_dates(): R20 preference (latest date >=3 days out first) ---
    dates = [iso(1), iso(4), iso(10), iso(2)]
    ranked = daemon.rank_dates(dates)
    check(
        "rank_dates: latest >=3-day-out date ranked first",
        ranked[0] == iso(10),
        f"got {ranked}",
    )
    check(
        "rank_dates: all 4 input dates present in output (no silent drops)",
        sorted(ranked) == sorted(dates),
        f"got {ranked}",
    )

    # --- rank_dates(): R20 fallback when nothing is >=3 days out ---
    near_only = [iso(0), iso(1), iso(2)]
    ranked_near = daemon.rank_dates(near_only)
    check(
        "rank_dates: fallback to earliest-first when no date is >=3 days out",
        ranked_near[0] == iso(0),
        f"got {ranked_near}",
    )

    # --- rank_dates(): R22 - a full ordering across ALL dates, not just top pick ---
    # This is what lets the polling loop try every other date in one cycle
    # before giving up, per R22, rather than picking one date and stopping.
    mixed = [iso(0), iso(15), iso(1), iso(20), iso(2)]
    ranked_mixed = daemon.rank_dates(mixed)
    check(
        "rank_dates: R22 full ordering - good dates (>=3d out) descending, then fallback ascending",
        ranked_mixed == [iso(20), iso(15), iso(0), iso(1), iso(2)],
        f"got {ranked_mixed}",
    )

    # --- rank_dates(): malformed entries are skipped, not fatal ---
    check(
        "rank_dates: malformed date strings are silently skipped",
        daemon.rank_dates(["not-a-date", iso(10)]) == [iso(10)],
    )

    # --- choose_time(): R21 preference for 12:00-14:00 ---
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
    # Turbo-stream-wrapped variant (see spec §5's note on this)
    turbo_html = (
        '<turbo-stream action="update" target="x"><template>'
        '<input name="broneering[_token]" value="xyz789">'
        '</template></turbo-stream>'
    )
    check(
        "extract_token: finds a token nested inside a turbo-stream/template",
        http_flow.extract_token(turbo_html) == "xyz789",
    )

    # --- estonia_booking_http.is_seat_dead(): R13's positive death signature ---
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
        # Round 2's "forced + identical dates -> suspect -> reacquire" tests
        # tested behaviour that round 3 deliberately removed (see
        # IMPLEMENTATION_CHANGELOG.md §9) - replaced below with debounce tests.
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
    # renewal machinery/lock for both reasons (R14 concurrency cap shared) ---
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

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
