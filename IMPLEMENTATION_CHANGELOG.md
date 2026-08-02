# Implementation Changelog — Parked-Session Booker Daemon

Implemented by: Claude (Sonnet 5, Cowork session) &nbsp;&nbsp; Date: 2026-08-02 &nbsp;&nbsp; Branch: `gcp-booker` &nbsp;&nbsp; Commits: *not committed — see §3, "Deviations from spec"*

## 0. Environment this was implemented under (read before trusting any test result below)

This session ran in a sandboxed workspace with **no network egress to
`broneering.mfa.ee`** (confirmed: `curl` to the live site timed out with no
route), **no `CAPSOLVER_API_KEY`**, and **no `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`**.
That means everything requiring the live site, a real CAPTCHA solve, or a
real Telegram send (T2–T9 in the spec's test plan) could not be executed
here and must be run for real, against Abu Dhabi/Cairo/Astana per spec §9,
before this is trusted in production. This is stated plainly rather than
faked — see §5 and §7 for exactly what was and wasn't exercised.

## 1. Requirement status

| ID | Requirement (short) | Status | Where | Notes |
|----|--------------------|--------|-------|-------|
| R1 | Don't modify `estonia_visa_monitor.py` | Done | — | `git diff --stat estonia_visa_monitor.py` shows nothing (see §5, T-confirm). |
| R2 | Cron cadence / Telegram+Twilio unaffected | Done | `estonia_visa_monitor_booker_hook.py:140` | Trigger-file write happens alongside, not instead of, the existing `mon.send_availability_alert()` call; monitor/cron files untouched. |
| R3 | Daemon crash can't affect monitor | Done | `estonia_booker_daemon.py` (whole file is a separate process) | Own lock (`estonia_booker_daemon_<ID>.lock`), own log (`daemon.log`), own state files. Hook's trigger write is independently try/excepted (R6). |
| R4 | Daemon stopped/never started ⇒ unchanged behaviour | Done | `estonia_visa_monitor_booker_hook.py:51-79` | `write_daemon_trigger()` writes a file nobody reads if the daemon isn't running; zero other behavior change. |
| R5 | Hook writes `.state/trigger_<ID>.json` atomically | Done | `estonia_visa_monitor_booker_hook.py:51-79` | temp file + `os.replace()`; smoke-tested (§5). |
| R6 | Trigger write wrapped, can't affect alerting | Done | `estonia_visa_monitor_booker_hook.py:56-78` | Whole body in try/except; on failure logs via `mon.log_line()` and returns, never raises. |
| R7 | Daemon polls trigger file every 0.5s, treats newer ts as fresh | Done | `estonia_booker_daemon.py:715-733` (`TriggerWatcher`), main loop at `:917+` | `TRIGGER_POLL_INTERVAL_SECONDS = 0.5`, not env-overridable per spec (fixed cadence, not a tunable). |
| R8 | No independent site check by default; `DAEMON_SELF_DETECT=1` opt-in | Done | `estonia_booker_daemon.py:737-750` (`self_detect_once`), gated in main loop | Default off; runs at most every 10s when enabled. |
| R9 | On trigger, acquire a seat if none live: CAPTCHA + steps 0-2 concurrent, then 3-4 | Partial | `estonia_booker_daemon.py:` `SeatManager.acquire` (~627-655) | Solves CAPTCHA and then calls `establish_seat()` (which itself does steps 0-4). The CAPTCHA solve is **not** run concurrently with steps 0-2 in this implementation — it's solved first, then steps 0-4 run. See §3 for why and the fix needed. |
| R10 | Operate on a 20-min seat; `SEAT_TTL_SECONDS`/`SEAT_RENEW_AFTER_SECONDS` env vars | Done | `estonia_booker_daemon.py:99-100` (config), `SeatManager.maybe_renew`/`check_ttl_expiry` (~657-704) | Defaults 1440/1200 exactly per spec. |
| R11 | Overlapping renewal: acquire new seat before dropping old, verify, atomic swap | Done | `SeatManager.maybe_renew` → `_do_renew()` (~665-693) | `establish_seat()` itself only returns on reaching the calendar (raises otherwise), which is the "verify" step; swap is a single reference assignment. |
| R12 | Only renew if D-visa still offered; else let old seat expire | Done | `_do_renew()`, the `check_service_offered` call before soving a CAPTCHA | Logs `INFO` and returns without touching `self.seat` if absent. |
| R13 | Seat death = token present, no `kuupaev` field. Never infer from empty slots. | Done | `estonia_booking_http.py:156-167` (`is_seat_dead`) | Unit-tested (§5) including the "checkboxes present but no date field" edge case, which must NOT be misread as dead. |
| R14 | ≤1 live seat + ≤1 in-flight replacement | Done | `SeatManager._renewal_lock` / `_renewal_in_progress` (~618-620, checked in `maybe_renew`) | A lock + boolean flag; a second `maybe_renew()` call while one is in flight is a no-op. |
| R15 | Poll free-times every `SLOT_POLL_INTERVAL_SECONDS` (default 2) | Done | `poll_cycle()` (~844+), called every main-loop iteration (~0.5s) while a seat is live | **Deviation, logged in §3**: the poll cadence is effectively the main loop's own 0.5s cadence, not a separate 2s timer — see §3 for why this is actually fine (and arguably better) but is not literally what R15 describes. |
| R16 | Query dates sequentially, not in parallel | Done | `poll_cycle()`, plain `for date_iso in rank_dates(...)` loop | No threading/async anywhere in the query path. |
| R17 | Resolve: can dates refresh in a live seat without a new CAPTCHA? | Done (implemented) / **Unverified live** | `estonia_booking_http.py:323-401` (`resolve_r17_refresh_dates`) | See §4 — implemented via step-4 replay, but could not be confirmed against the live site in this session. **Must be verified before trusting.** |
| R18 | Empty poll result is normal, not an error | Done | `poll_cycle()`, `if not slots: continue` | Logged as `POLL_RESULT` with `slots=0`, no alert. |
| R19 | Book immediately on ≥1 slot, no extra round trips | Done | `poll_cycle()` → `attempt_booking()` called directly on first non-empty `slots` | |
| R20 | Date preference: latest ≥3 days out, fallback earliest | Done | `estonia_booker_daemon.py:501-522` (`rank_dates`) | Unit-tested (§5). |
| R21 | Time preference: earliest 12:00-14:00, fallback earliest overall | Done | `estonia_booker_daemon.py:524-538` (`choose_time`) | Unit-tested (§5). |
| R22 | Multi-date fallback: try every other date before giving up | Done | `poll_cycle()`'s `for date_iso in rank_dates(...)` loop continues past a date with zero slots or a failed booking attempt | `rank_dates()` returns a full preference ordering (not just a top pick) specifically so this loop can walk every date in one cycle. |
| R23 | Booking needs CAPTCHA; use reserve, else solve on demand + log loudly | Done | `attempt_booking()` (~757+), `TokenReserve.take()` | Logs `"Token reserve empty/stale at booking time - solving on demand..."` at INFO before falling back. |
| R24 | On confirmed booking: set `booked` flag, Telegram success, stop attempts, keep monitoring | Done | `attempt_booking()`, `mark_booked()` (~239-247) | Daemon keeps running/polling after a booking (no `sys.exit`); `is_already_booked()` short-circuits all future attempts. |
| R25 | Never book twice — check flag before every submit | Done | `attempt_booking()` line 1 (`if is_already_booked(): ... return True`), also re-checked at top of `poll_cycle()` | Checked per-attempt, not just at startup. |
| R26 | `DAEMON_DRY_RUN=1`: full flow, stop before submit, log + Telegram intent | Done | `attempt_booking()`, the `if DAEMON_DRY_RUN:` branch before `submit_booking()` is ever called | Smoke-tested: daemon starts and idles cleanly under `DAEMON_DRY_RUN=1` (§5); the stop-before-submit branch itself could not be exercised end-to-end without a live seat. |
| R27 | Keep one fresh token while seat live, refresh every `TOKEN_REFRESH_SECONDS` (75) | Done | `estonia_booker_daemon.py:545-604` (`TokenReserve`) | Background thread started in `SeatManager.acquire()`, stopped in `drop_seat()`. |
| R28 | Reserve only runs while a seat is live | Done | `SeatManager.acquire()` calls `token_reserve.start()`; `drop_seat()`/`check_ttl_expiry()` call `token_reserve.stop()` | |
| R29 | Never use a token older than `TOKEN_MAX_AGE_SECONDS` (100) | Done | `TokenReserve.take()` (~592-604) | Logs `TOKEN_STALE` and discards if `age > TOKEN_MAX_AGE_SECONDS`. |
| R30 | Reserve = CapSolver only, never `race`; `race` reserved for on-demand fallback | Done | `TokenReserve._run()` calls `solve_capsolver()` directly (not `solve_on_demand()`); `solve_on_demand()` (the only place `race` is honoured) used for acquisition/renewal/on-demand booking fallback | |
| R31 | Cap solves at `MAX_SOLVES_PER_HOUR` (60); on breach stop + Telegram, keep monitor/seat | Done | `estonia_booker_daemon.py:316-352` (`SolveRateLimiter`), called from `solve_capsolver`/`solve_2captcha` | Rolling 3600s window via `deque`, not wall-clock-hour buckets. Raising `RateLimitExceeded` is caught wherever solves are attempted so it never crashes the loop. |
| R32 | Poll CapSolver balance every 15 min; alert below `CAPSOLVER_LOW_BALANCE_USD` | Done | `estonia_booker_daemon.py:463-494` (`check_capsolver_balance`), called from main loop every `BALANCE_CHECK_INTERVAL_SECONDS` | Could not be exercised against the real API (no key) — request shape follows the spec's documented endpoint exactly. |
| R33 | All daemon output to `daemon.log`, UTC ISO-8601, one line per event | Done | `estonia_booker_daemon.py:126-144` (`log_line`) | Confirmed by smoke test (§5): `daemon.log` created and populated. |
| R34 | Log every named state transition with elapsed-since-trigger | Done | Every `SEAT_*`, `TRIGGER_RECEIVED`, `POLL_RESULT`, `SLOT_FOUND`, `BOOKING_*`, `TOKEN_*`, `BALANCE_LOW`, `RATE_LIMIT_HIT` call site passes `elapsed_since_trigger` where a trigger context exists | Some transitions (e.g. `SEAT_EXPIRED`, `TOKEN_REFRESHED`) don't have a meaningful "elapsed since trigger" at the moment they fire (they can happen between episodes) and omit it — this is intentional, not an oversight. |
| R35 | `daemon_polls.csv`: utc, seat_age_s, date, slot_count, slot_times, query_ms, seat_alive | Done | `estonia_booker_daemon.py:162-177` (`append_poll_csv`), called from `poll_cycle()` | Column order matches spec exactly. |
| R36 | On booking failure, dump full HTML to `daemon_dumps/`, reference path in log | Done | `estonia_booker_daemon.py:147-158` (`dump_html`), called in `attempt_booking()`'s unclear-confirmation branch | |
| R37 | Telegram on the specific listed events only, never routine polls | Done | `send_telegram()` calls exist at: seat acquired, booking success, booking failure/unclear, CAPTCHA-solve failure at booking time, balance low, rate limit hit, daemon start, daemon clean shutdown | No `send_telegram()` call anywhere in `poll_cycle()`'s per-date loop itself. |
| R38 | Reuse `RUNTIME_LABEL` signature convention | Done | `estonia_booker_daemon.py:113` (`RUNTIME_SIGNATURE`) | `f"\n\n— Sent from {RUNTIME_LABEL} booker daemon"`, same pattern as the monitor/old booker. |
| R39 | systemd unit `estonia-booker-daemon.service`, `Restart=always`, `RestartSec=10` | Done | `estonia-booker-daemon.service` | See §3 for the one deviation (env sourcing mechanism) and why. |
| R40 | Handle SIGTERM cleanly: finish in-flight booking, log shutdown, exit 0 | Done | `estonia_booker_daemon.py:912-915` (`_handle_sigterm`), `main()`'s `finally` block | Live-tested (§5): sent SIGTERM via `timeout`, confirmed clean log lines and lock removal. "Finish in-flight booking" is honored by the fact the signal only sets a flag checked between loop iterations, never interrupts mid-iteration. |
| R41 | Single-instance lock | Done | `estonia_booker_daemon.py:273-311` (`acquire_daemon_lock`/`release_daemon_lock`) | Includes stale-lock (dead-PID) recovery, unlike the old booker's simple age-based staleness — tested indirectly (lock created and removed cleanly in the smoke test, §5). |
| R42 | Document every new env var in `BOOKER_README.md`, with defaults | Done | `BOOKER_README.md`, "Parked-Session Booker Daemon" section | All of §6.9's table plus `CAPTCHA_PROVIDER`/`TWOCAPTCHA_API_KEY` (needed for the on-demand fallback, R23/R30) are documented with defaults. |

## 2. Files

| File | New/Modified | Purpose | Lines |
|------|--------------|---------|-------|
| `estonia_booking_http.py` | New | Reusable, side-effect-free (besides HTTP) implementation of steps 0-7: `establish_seat`, `check_service_offered`, `query_free_times`, `resolve_r17_refresh_dates`, `submit_booking`, `delete_booking`, `is_seat_dead`. | 1-522 |
| `estonia_booker_daemon.py` | New | The daemon: config, logging/CSV/Telegram, booked-flag state, lock, CAPTCHA solving + rate limiting + balance check, date/time preference, token reserve, seat manager, trigger watcher, booking, main loop. | 1-935 |
| `estonia_visa_monitor_booker_hook.py` | Modified | Added `write_daemon_trigger()` and the call to it at the top of the `if found:` branch in `main()`. Nothing else in this file changed. | +47 lines (trigger helper at 22-79; call site at 140) |
| `estonia-booker-daemon.service` | New | systemd unit (R39). | 1-33 |
| `estonia-booker-daemon.logrotate` | New | logrotate config for `daemon.log`/`daemon_polls.csv`, mirroring the monitor's own. | 1-13 |
| `test_daemon_units.py` | New | T1: unit tests for `rank_dates`, `choose_time`, `extract_token`, `is_seat_dead`. | 1-140 |
| `BOOKER_README.md` | Modified | Added the "Parked-Session Booker Daemon" section documenting the daemon, its files, env vars, and operations. | New section, ~140 lines |
| `IMPLEMENTATION_CHANGELOG.md` | New | This file. | — |
| `estonia_visa_monitor.py` | **Untouched** | R1 — confirmed via `git diff --stat` (§5). | — |

## 3. Deviations from spec

**R9 — CAPTCHA solve not run concurrently with steps 0-2.** The spec's
timeline (§4.3) shows "Daemon starts a CAPTCHA solve AND begins the HTTP flow
in parallel." `SeatManager.acquire()` instead calls `check_service_offered()`
(steps 0-1, ~0.5-1s), then `solve_on_demand()` (blocking, ~15-25s), then
`establish_seat()` (steps 0-4 again, from scratch, in a fresh session).
This is **strictly slower** than the spec's intended design by roughly the
0.5-1s of the first `check_service_offered()` call, and — more importantly —
re-runs steps 0-1 twice (once for the gate check, once inside
`establish_seat()`) rather than reusing the same session/token across both.
Why this was done: threading the CAPTCHA solve together with a session that
also needs to carry forward a rotating CSRF token across the solve's ~20s
window is fiddly to get right blind (no live site to test the timing
against, see §0), and a strictly-correct-but-slightly-slower acquisition
felt like the safer failure mode for a component this is easy to get
subtly wrong in a race. **This should be fixed** by threading the CAPTCHA
solve alongside `establish_seat()`'s own steps 0-2 (which don't need the
token) and only blocking on the solve result at step 3, using the *same*
session throughout — but doing that correctly needs a live seat to verify
against, which this session didn't have. Flagged here rather than done
silently.

**R15 — poll cadence is the main loop's cadence (~0.5s), not a dedicated 2s
timer.** `poll_cycle()` runs on every main-loop iteration while a seat is
live, and the main loop itself runs at the R7-mandated 0.5s trigger-poll
cadence. This means slots are actually queried roughly every 0.5s per date
in this implementation, not every `SLOT_POLL_INTERVAL_SECONDS=2` as the
variable name implies. This is arguably *better* for the spec's stated goal
(latency), and R15 doesn't forbid polling faster than the default - it just
specifies what the default interval should be - but it means
`SLOT_POLL_INTERVAL_SECONDS` is currently **not actually wired into a sleep
anywhere** and is effectively dead configuration. This should either be (a)
removed as a no-op env var with a note explaining the actual cadence, or (b)
properly wired as a separate timer decoupled from the trigger-poll loop so
the two cadences (trigger-checking vs. slot-polling) can genuinely be tuned
independently, which was the spec's evident intent. Left as-is rather than
guessed-and-fixed because changing the timing structure without a live seat
to observe real request latency against felt like the wrong kind of change
to make blind.

**systemd unit's environment sourcing (R39).** The spec says "same env vars
as `run_monitor.sh`." `run_monitor.sh` is not in git and, per `AI_CONTEXT.md`,
ends by synchronously invoking the monitor itself - so this repo cannot ship
a copy of it, and the unit cannot just source it directly without also
launching the monitor from inside the daemon's own service. The unit instead
documents extracting only the `export` lines into a new file,
`run_booker_daemon_env.sh` (also not in git), and sourcing that. This is a
reasonable reading of "same env vars," not a shortcut, but it does mean a
one-time manual step is required on the VM before `systemctl enable` works -
documented in `BOOKER_README.md`.

**Git commits.** The spec asks for commit hashes in this changelog's header.
This implementation session's sandbox has a `.git/index.lock` file that
cannot be removed or written past (`PermissionError: Operation not
permitted`, confirmed directly rather than assumed) - `git add`/`git commit`
cannot be run here. All new/modified files are present in the working tree
on the `gcp-booker` branch and ready to commit; the operator running this on
a machine without that restriction should `git add` the files listed in §2
and commit before pushing/deploying.

**Two unrelated pre-existing working-tree changes.** `git status` at the
start of this session showed `AI_CONTEXT.md` and `applicant_data_dummy.json`
already modified before any work in this session began (confirmed via `git
diff --stat` showing changes unrelated to anything in this spec). Neither
was touched further by this implementation; noted here only so they aren't
mistaken for something this session did.

## 4. Open question R17 — date-list refresh within a seat

**Finding:** implemented, but **unverified against the live site** — could
not be tested in this session (see §0: no network egress to
`broneering.mfa.ee` at all in this sandbox, confirmed via a direct `curl`
timeout).

**Approach taken:** `resolve_r17_refresh_dates()` in `estonia_booking_http.py`
re-POSTs the applicant-details step (step 4) using the applicant data cached
on the `Seat` object at acquisition time (`cache_applicant_on_seat()`). Step
4's original request carries no `g-recaptcha-response` field at all in the
documented flow (spec §5) - it sits downstream of the CAPTCHA gate (step 3),
not behind its own gate - so replaying it with identical data should, if the
server recomputes `data-vastuvotukuupaevad` on every render of that step
(rather than baking it in once, at the moment the seat was first
established), return a fresh date list at zero CAPTCHA cost. The daemon
calls this once per poll cycle, before querying individual dates, so a date
that newly appears in a later availability episode (F9) becomes visible to
an already-parked seat.

**Evidence supporting the choice:** this is the exact fallback the spec
itself suggests in §6.4/R17 ("e.g. re-POSTing step 4, or a plain GET
`/en/` within the session"). Step 4 is documented (spec §5, step 4) as
taking only applicant fields and the CSRF token - no CAPTCHA-related field -
which is why it was chosen over the plain-GET alternative: a plain GET
returns the pre-embassy-selection landing page, which would need embassy +
service re-selection (steps 1-2, both CAPTCHA-free per F2) replayed just to
get back to a state where the calendar element exists at all — a heavier,
multi-request fallback compared to a single step-4 replay.

**What is NOT yet known:** whether the live server actually recomputes
`data-vastuvotukuupaevad` on a step-4 replay, or whether it returns the
same cached list from session-establishment time regardless (in which case
this "refresh" would silently be a no-op that always looks like it worked -
same request succeeding, same-looking response - while never actually
picking up new dates). The `is_seat_dead()` check IS applied to every
response from this replay, so a genuinely expired seat is still caught
correctly either way; what's unverified is specifically whether the *date
list itself* updates.

**Resulting design decision:** ship this implementation, but treat R17 as
**not closed**. The very first live test against Abu Dhabi (spec T2/T3)
should specifically watch whether `resolve_r17_refresh_dates()`'s returned
date list ever differs from the list at acquisition time across a poll
session long enough to plausibly span two availability episodes - if it
never changes even when the site's own dropdown would suggest new
inventory, that confirms the step-4 replay does NOT refresh dates, and the
plain-GET-plus-steps-1-2-replay fallback described above needs to be
implemented and swapped in instead. This is exactly the "stop and flag"
condition spec §11 step 3 calls for, surfaced here rather than silently
assumed to work.

## 5. Test results

| ID | Command run | Result | Evidence |
|----|-------------|--------|----------|
| Compile check | `python3 -c "import py_compile; py_compile.compile('estonia_booking_http.py', doraise=True); py_compile.compile('estonia_booker_daemon.py', doraise=True); py_compile.compile('estonia_visa_monitor_booker_hook.py', doraise=True)"` | Passed | Output: `COMPILE OK`, no exceptions. |
| T1 (unit tests) | `python3 test_daemon_units.py` | **All 16 checks passed** | Full output:<br>`[PASS] rank_dates: latest >=3-day-out date ranked first`<br>`[PASS] rank_dates: all 4 input dates present in output (no silent drops)`<br>`[PASS] rank_dates: fallback to earliest-first when no date is >=3 days out`<br>`[PASS] rank_dates: R22 full ordering - good dates (>=3d out) descending, then fallback ascending`<br>`[PASS] rank_dates: malformed date strings are silently skipped`<br>`[PASS] choose_time: prefers earliest slot in 12:00-14:00`<br>`[PASS] choose_time: falls back to earliest overall when nothing is in 12:00-14:00`<br>`[PASS] choose_time: returns None for an empty list`<br>`[PASS] choose_time: skips unparseable labels rather than crashing`<br>`[PASS] extract_token: finds a plain <input> token`<br>`[PASS] extract_token: returns None when absent`<br>`[PASS] extract_token: finds a token nested inside a turbo-stream/template`<br>`[PASS] is_seat_dead: a response with both token AND date field is ALIVE`<br>`[PASS] is_seat_dead: token present but date field gone => DEAD (R13 signature)`<br>`[PASS] is_seat_dead: checkboxes present (even without the date input) is NOT treated as dead`<br>`[PASS] is_seat_dead: no token at all is NOT the R13 signature (that's a different failure - markup change)`<br>`ALL TESTS PASSED` |
| Trigger-file smoke test (R5/R6) | Python one-liner monkeypatching `hook.STATE_DIR` to a tempdir, calling `hook.write_daemon_trigger([...])` | Passed | `exists: True`; `content: {'ts': 1785674275.17..., 'services': ['Long-stay visa (D-visa) application']}` — confirms atomic write + correct JSON shape. |
| Daemon startup/shutdown smoke test (R33, R40, R41) | `EMBASSY_ID=244 EMBASSY_NAME="Abu Dhabi" DAEMON_DRY_RUN=1 timeout 4 python3 estonia_booker_daemon.py` (no CAPSOLVER/Telegram creds set) | Passed | Log output:<br>`[INFO] Daemon starting. embassy=244/Abu Dhabi dry_run=True self_detect=False captcha_provider=capsolver`<br>`[INFO] Telegram not configured, skipping send: ...` (expected - no creds in this sandbox)<br>`[INFO] Received signal 15 - finishing any in-flight work then shutting down (R40)`<br>`[INFO] Daemon shutting down cleanly (R40)`<br>Confirmed after exit: `/tmp/estonia_booker_daemon_244.lock` removed (R41), `daemon.log` created and populated (R33), `daemon_polls.csv` correctly NOT created (no seat was ever live, so nothing to poll - R35 firing only on actual polls, not unconditionally). |
| `estonia_visa_monitor.py` untouched (R1 confirmation) | `git diff --stat estonia_visa_monitor.py` | Passed | Empty output — no diff at all. |
| Branch check | `git branch --show-current` | Passed | `gcp-booker` |
| **T2 (seat acquisition vs. Abu Dhabi)** | *Not run* | **Not run — see §0/§6** | No network egress to the live site in this sandbox. |
| **T3 (5-min poll loop)** | *Not run* | **Not run** | Same reason. |
| **T4 (overlapping renewal)** | *Not run* | **Not run** | Same reason. |
| **T5 (seat-death detection past 1440s)** | *Not run* | **Not run** | Same reason; the *detection logic itself* (`is_seat_dead`) is unit-tested above, but not against a real 1440s-expired session. |
| **T6 (`DAEMON_DRY_RUN=1` end to end)** | *Not run* | **Partially covered** | Daemon starts cleanly under `DAEMON_DRY_RUN=1` (above), but the actual stop-before-submit code path (`attempt_booking()`'s dry-run branch) was never exercised, since no seat/slot was ever live to reach it. |
| **T7 (real submit+delete)** | *Not run* | **Not run** — do not run this against production without a live test first anyway | Same reason. |
| **T8 (kill daemon mid-poll, confirm monitor unaffected)** | *Not run* | **Not run** | Requires a live poll in progress, which requires network access this sandbox doesn't have. |
| **T9 (invalid `CAPSOLVER_API_KEY` ⇒ graceful degradation)** | *Not run* | **Not run** | Requires attempting a real API call, which requires network access. |

## 6. Unresolved / not implemented

- **T2–T9 must be run for real** against Abu Dhabi (`244`), Cairo (`45`), or
  Astana (`49`) before this is trusted operationally - see §0 and §5 for
  exactly what blocked them here (no network egress, no CAPSOLVER/Telegram
  credentials in the implementation sandbox).
- **R9's CAPTCHA-solve/HTTP-flow concurrency** is implemented sequentially
  rather than in parallel — see §3 for the reasoning and the fix needed.
  This makes acquisition somewhat slower than the spec's ~20s target
  (roughly +0.5-1s from the redundant steps 0-1 call), not incorrect, but
  worth fixing once there's a live seat to verify the threaded version
  against.
- **`SLOT_POLL_INTERVAL_SECONDS` is currently dead configuration** — see §3.
  Should be either wired to an actual independent timer or removed/renamed
  to avoid the false impression that changing it changes anything.
- **R17 is implemented but not verified** — this is the single most
  important open item; see §4 in full.
- **CapSolver balance-check request shape (R32)** was written to match the
  spec's documented endpoint exactly but has never been called against the
  real API in this session — worth a first manual smoke-test call with a
  real key before relying on the alert.

## 7. Reviewer notes

**Least confident about, in order:**

1. **R17** (§4) — whether the chosen no-CAPTCHA date-refresh mechanism
   actually refreshes anything, versus silently no-op'ing while looking
   like it worked. This is the one place a bug could hide completely
   silently (no exception, no error signature — see the SeatDeadError check
   still passing even if the *date list itself* is stale) until a real
   second-episode date is missed. **Verify this first**, before anything
   else, exactly as spec §11 step 3 instructs.
2. **R9's acquisition sequencing** (§3) — functionally correct (steps run in
   the right order, gate-checked, CAPTCHA-required) but slower than the
   spec's intended design and duplicates steps 0-1. Not dangerous, just not
   optimal — worth fixing once a live seat exists to time it against.
3. **Everything in `estonia_booking_http.py`'s request shapes** (form field
   names, header overrides, CSRF token extraction) is copied directly from
   `probe_http_flow.py`/`test_session_longevity.py`, both of which the spec
   describes as "proven, tested code" from real measurements on 2026-08-02 -
   but this session could not re-run either of those probes itself to
   reconfirm the site hasn't changed shape since. If T2 fails on first real
   run, check this before assuming the daemon's own logic is at fault.
4. **The booked-flag file sharing** between this daemon and the old
   Playwright booker (`estonia_d_visa_booker.py`) via the identical
   `.state/booker_state_<ID>.json` path was a deliberate design choice (§2 of
   this changelog, R24/R25) to prevent double-booking between the two
   systems, but was never tested with both actually running concurrently
   against the same embassy — worth a dedicated test if both are ever
   enabled at once in practice (the spec's non-goals say the old booker
   stays as a fallback, which implies exactly this scenario is possible).
5. **Rate limiting and balance-check** (R31/R32) logic is straightforward
   and unit-testable in isolation but was only exercised via code reading in
   this session, not by actually driving the rate limiter past its
   threshold or hitting a real low-balance response - worth a quick manual
   test (e.g. temporarily setting `MAX_SOLVES_PER_HOUR=1` against a real
   key) before trusting the alert path in production.

## 8. Follow-up fixes (round 2)

Implemented by: Claude (Sonnet 5, Cowork session) &nbsp;&nbsp; Date: 2026-08-02 &nbsp;&nbsp; Branch: `gcp-booker` &nbsp;&nbsp; Commits: *not committed — same sandbox `.git/index.lock` permission restriction as round 1 (§3 above); all changes are in the working tree, ready for the operator to `git add`/commit on a machine without that restriction.*

Same sandbox constraints as round 1 apply here too: **no network egress to `broneering.mfa.ee`, no `CAPSOLVER_API_KEY`, no Telegram credentials.** Per this task's explicit instructions, that's actually fine this round - none of Fixes 1-6 require touching the live site, and the constraints explicitly forbid making any live request or booking anything. Everything below was either unit-tested with fake clocks/monkeypatches (no network) or verified by direct code reading plus a `DAEMON_DRY_RUN=1` startup/shutdown smoke test (also no network, since no trigger file existed to make it try acquiring a seat).

### Fix table

| Fix | Description | Status | File:line | Notes |
|-----|--------------|--------|-----------|-------|
| 1 | Decouple slot-poll cadence (`SLOT_POLL_INTERVAL_SECONDS`) from the 0.5s main loop | Done | `estonia_booker_daemon.py:1117` (main loop gating), `SeatManager.__init__` `:623` / `acquire()` reset `:652` / `_do_renew()` swap reset `:712` / `drop_seat()` reset `:742` (scheduling-state resets) | Main loop still runs every 0.5s for R7 trigger detection (untouched); `poll_cycle()` is now only invoked when `time.time() - seat_mgr.last_slot_poll_ts >= SLOT_POLL_INTERVAL_SECONDS`. `last_slot_poll_ts` starts/resets at `0.0` on every fresh/swapped seat so the very first tick after acquisition still polls immediately (matches spec §4.3's "if a slot is already there, book it now"). |
| 2 | Separate date-refresh cadence (`DATE_REFRESH_INTERVAL_SECONDS`, new, default 10s) from the slot-poll cadence | Done | `estonia_booker_daemon.py:86` (config), `maybe_refresh_dates()` `:880-953`, called from `poll_cycle()` with `forced=False` | Between refreshes, `seat.available_dates` (the cached list from the last refresh) is reused as-is - no request made. Confirmed by unit test (see §8's "Test results" below): "not forced, interval not yet elapsed" makes zero calls to `resolve_r17_refresh_dates`. |
| 3 | Global hard outbound rate ceiling, no call path can bypass it | Done | `estonia_booking_http.py:87-224` (`RateLimiter` class, `_RATE_LIMITER` singleton, `_throttled()` helper); every `session.get`/`session.post` call site in the file now routes through `_throttled(...)` - verified via `grep -n "session\.\(get\|post\)(" estonia_booking_http.py \| grep -v _throttled` returning zero matches (see "Test results") | Token bucket, blocks (never drops), `MAX_REQUESTS_PER_SECOND` (default 1.0) / `REQUEST_BURST_ALLOWANCE` (default 3), both env-overridable. `RATE_THROTTLED` logged at most once per 30s via a standalone `_log()` helper in this module (documented deviation from the module's original "no logging side effects" design - see "Deviations" below). |
| 4 | Fix the seat-acquired Telegram message's cadence claim | Done | `estonia_booker_daemon.py:664` | Now reads "polling free-times every {SLOT_POLL_INTERVAL_SECONDS}s, refreshing the date list every {DATE_REFRESH_INTERVAL_SECONDS}s (immediately instead, whenever a new trigger arrives)" - accurate after Fixes 1/2/5. Not live-tested (no Telegram credentials in this sandbox) - reviewed by reading the f-string and cross-checking against the actual gating logic. |
| 5 | Force immediate date refresh + suspect detection on a new trigger while a seat is already live | Done | `estonia_booker_daemon.py:880-953` (`maybe_refresh_dates`, the `forced=True` branch), wired at the trigger-handling call site `:1089-1096` (`elif seat_mgr.has_live_seat()...`) | Compares the freshly refreshed list against `seat_mgr.dates_at_acquisition` (the list captured when this seat was established/last swapped - see Fix 1's line refs). Empty-or-identical ⇒ logs `DATE_REFRESH_SUSPECT`, Telegrams, `drop_seat()` + `acquire()` (one CAPTCHA). Every refresh (forced or not) is recorded in `daemon_polls.csv` via a `__DATE_REFRESH__` pseudo-date row (`append_poll_csv` call inside `maybe_refresh_dates`). |
| 6 | R9 concurrency: CAPTCHA solve alongside steps 0-2, block only at step 3 | **Skipped** | — | See "Deviations" below for the full reasoning - this needed splitting `establish_seat()`'s public API in `estonia_booking_http.py` (a correctness-sensitive change to the proven HTTP flow) with no way to verify the result against the live site in this sandbox. The task's own instruction ("do NOT half-do it... skip if you can't do it cleanly") was explicit enough that skipping and documenting felt like the correct call rather than shipping an unverified refactor of the request flow. |

### Before/after estimated request rate while a seat is live

**Before (round 1):** the main loop ran every `TRIGGER_POLL_INTERVAL_SECONDS = 0.5s`, and both the slot-poll (`poll_cycle()`, which itself issues 1 request per available date via `query_free_times`) and the R17 date-refresh (`resolve_r17_refresh_dates`, 1 request) ran on *every* iteration, unconditionally, whenever a seat was live.

For the common case of 1 available date (matches every real New Delhi trigger on record per the spec - "New Delhi offered exactly one date each time", §3 "Dead hypotheses" table):

```
requests per main-loop tick = 1 (date refresh) + 1 (slot query for the one date) = 2
ticks per second            = 1 / 0.5s = 2
requests/second             = 2 requests/tick × 2 ticks/s = 4 requests/second
```

(The task description's own estimate of "~2 requests/second" is also consistent with this if you count only the slot-poll side, or with a slightly different date count - either way, the point stands: it was running unthrottled at the main loop's own 2 Hz tick rate, times however many requests each tick made.)

**After (round 2, Fixes 1+2):**

```
slot-poll requests/second  = (1 request per date) / SLOT_POLL_INTERVAL_SECONDS(2s)  = 0.5 req/s
date-refresh requests/sec  = 1 request / DATE_REFRESH_INTERVAL_SECONDS(10s)          = 0.1 req/s
------------------------------------------------------------------------------------------------
steady-state total         ≈ 0.6 requests/second while a seat is live
```

This matches the task's own "~0.6 requests/second" target exactly for the 1-date case. With N available dates it becomes `N/2 + 0.1` req/s (N/2 from slot-polling each date every 2s, plus one shared date-refresh request every 10s regardless of N) - still a large reduction from the old unconditional-every-0.5s-tick behaviour, which scaled as `2×(N+1)` req/s.

**Fix 5's forced refreshes** add at most 1 extra request per NEW trigger file event (not per main-loop tick) - these are genuinely rare (bursts of triggers separated by minutes, per F9), so they don't materially change the steady-state average; they're a deliberate, bounded exception to the cadence, not a leak back into high-frequency polling.

**Fix 3's rate ceiling (`MAX_REQUESTS_PER_SECOND=1.0` default, burst 3)** sits underneath both of the above as a hard cap - the arithmetic above (≈0.6 req/s steady state, occasional 1-2 req/s bursts around a forced refresh or seat acquisition's multi-step flow) stays comfortably under the 1.0 req/s ceiling in the common case, so this fix is not expected to visibly throttle anything in normal operation - its job is purely to catch a *future* bug, per the task's framing ("This is a safety net, not a tuning knob").

### Deviations from these instructions, and why

**Fix 3's `_log()` helper is a new logging side effect in `estonia_booking_http.py`.** The module's original docstring (round 1) states it deliberately has "NO Telegram/logging side effects of its own." The task explicitly requires `RATE_THROTTLED` to be logged, and doing so from the daemon's `log_line()` instead would require `estonia_booking_http.py` to import `estonia_booker_daemon.py` - which already imports `estonia_booking_http.py`, so that would be a circular import. A minimal standalone `_log()` (same timestamped-print style as everything else in this codebase) was added instead, scoped to this one event only. This is flagged as a deliberate, narrow exception in both the module docstring and here, not a silent scope-creep of the file's responsibilities.

**Fix 6 was skipped entirely** rather than partially implemented. The task instructions were explicit ("Do this ONLY if you can do it cleanly... Do not half-do it"). Making seat acquisition's CAPTCHA solve run concurrently with steps 0-2 (per spec §4.3's intended design) requires splitting `establish_seat()` in `estonia_booking_http.py` into at least two public pieces (a "steps 0-2, no CAPTCHA needed yet" function and a "steps 3-4, given a token" continuation) so `SeatManager.acquire()` can start the CAPTCHA thread, run the first piece, then block on the thread only before calling the second. That is a change to the proven, spec-cited HTTP flow (§5: "Reuse that code rather than rewriting from the tables here"), and this sandbox has no way to run it against the live site to confirm the split didn't subtly break token/session continuity across the two pieces (e.g. whether the CSRF token rotation still lines up correctly when the two pieces run with a time gap between them and a token solved in parallel). Given the task's own admission that "correctness matters far more than 1-2 seconds here," this was left as `Skipped` with this reasoning rather than shipped unverified. IMPLEMENTATION_CHANGELOG.md §3/§7's original R9 note stands: this is still open.

**No other deviations.** Fixes 1, 2, 4, and 5 were implemented as literally described in the task. Fix 3's token-bucket design (steady refill, blocking `acquire()`, burst allowance, throttled-wait logging capped at once per 30s) matches the task's bullet points one-for-one.

### `estonia_visa_monitor.py` untouched (constraint check)

```
$ git diff --stat estonia_visa_monitor.py
$
```

Empty output - confirmed no diff. This file was not opened for editing at any point in this round-2 session (only read indirectly via the changelog/spec references above).

### Test results

**Compile checks:**
```
$ python3 -c "import py_compile; py_compile.compile('estonia_booking_http.py', doraise=True); print('OK')"
OK
$ python3 -c "import py_compile; py_compile.compile('estonia_booker_daemon.py', doraise=True); print('OK')"
OK
```

**Confirmation every site-facing request routes through the rate limiter (Fix 3's "no call path can bypass it"):**
```
$ grep -n "session\.get(\|session\.post(\|\.session\.get(\|\.session\.post(" estonia_booking_http.py | grep -v "_throttled"
213:    Every `session.get(...)`/`session.post(...)` call to the live site in
```
(The one remaining match is a comment describing the mechanism, not a live call site - every actual `.get(`/`.post(` invocation is wrapped in `_throttled(...)`.)

**`test_daemon_units.py` (T1, extended this round with Fix 3's rate-limiter tests and Fixes 1/2/5's scheduling tests) - full real output:**

```
[PASS] rank_dates: latest >=3-day-out date ranked first
[PASS] rank_dates: all 4 input dates present in output (no silent drops)
[PASS] rank_dates: fallback to earliest-first when no date is >=3 days out
[PASS] rank_dates: R22 full ordering - good dates (>=3d out) descending, then fallback ascending
[PASS] rank_dates: malformed date strings are silently skipped
[PASS] choose_time: prefers earliest slot in 12:00-14:00
[PASS] choose_time: falls back to earliest overall when nothing is in 12:00-14:00
[PASS] choose_time: returns None for an empty list
[PASS] choose_time: skips unparseable labels rather than crashing
[PASS] extract_token: finds a plain <input> token
[PASS] extract_token: returns None when absent
[PASS] extract_token: finds a token nested inside a turbo-stream/template
[PASS] is_seat_dead: a response with both token AND date field is ALIVE
[PASS] is_seat_dead: token present but date field gone => DEAD (R13 signature)
[PASS] is_seat_dead: checkboxes present (even without the date input) is NOT treated as dead
[PASS] is_seat_dead: no token at all is NOT the R13 signature (that's a different failure - markup change)
[PASS] RateLimiter: burst allowance grants the first `burst` acquires with zero waiting
[PASS] RateLimiter: 4th acquire (bucket exhausted) waits exactly 1/rate=1.0s and advances the clock accordingly
[PASS] RateLimiter.measured_rate(): reports ~4 req in ~1s after the sequence above
[PASS] RateLimiter: RATE_THROTTLED logs exactly once for a burst of throttled acquires within one 30s log window
[PASS] RateLimiter: RATE_THROTTLED logs again once >=30s has passed since the last log
[PASS] RateLimiter: 10 sequential acquires against a slow rate all complete (blocks, never drops/raises)
[PASS] maybe_refresh_dates: not forced, interval not yet elapsed -> skipped, no refresh call, returns False
[PASS] maybe_refresh_dates: not forced, interval elapsed -> refresh IS called, returns False (nothing suspect for non-forced)
[PASS] maybe_refresh_dates: forced + refreshed dates identical to acquisition dates -> SUSPECT -> drops seat and reacquires
[PASS] maybe_refresh_dates: forced + EMPTY refreshed dates -> SUSPECT -> drops seat and reacquires
[PASS] maybe_refresh_dates: forced + a genuinely NEW date appears -> NOT suspect -> no reacquire, seat kept
[PASS] SeatManager: starts with last_slot_poll_ts=0.0 (so the very first main-loop tick after acquire polls immediately)
[PASS] SeatManager.drop_seat(): resets last_slot_poll_ts/last_date_refresh_ts/dates_at_acquisition

ALL TESTS PASSED
```

(25 checks total, 25 passed. One rate-limiter test initially failed on first write - advancing the fake clock directly by 30s also refills the token bucket for that elapsed time, so the very next `acquire()` succeeded without throttling at all, producing zero `RATE_THROTTLED` lines instead of the expected one. Fixed by draining that refilled token with an extra `acquire()` before the one meant to actually observe a throttled wait. Recorded here rather than silently corrected, per "match the previous session's standard" of showing real, including initially-wrong, results.)

**`DAEMON_DRY_RUN=1` startup/shutdown smoke test (confirms the changes don't break daemon startup, and that `estonia_visa_monitor.py`/lock/log behavior from round 1 still holds):**
```
$ EMBASSY_ID=244 EMBASSY_NAME="Abu Dhabi" DAEMON_DRY_RUN=1 timeout 4 python3 estonia_booker_daemon.py
2026-08-02T13:26:28+00:00 [INFO] Daemon starting. embassy=244/Abu Dhabi dry_run=True self_detect=False captcha_provider=capsolver
2026-08-02T13:26:28+00:00 [INFO] Telegram not configured, skipping send: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set
2026-08-02T13:26:32+00:00 [INFO] Received signal 15 - finishing any in-flight work then shutting down (R40)
2026-08-02T13:26:32+00:00 [INFO] Daemon shutting down cleanly (R40)
2026-08-02T13:26:32+00:00 [INFO] Telegram not configured, skipping send: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set
EXIT=0
```
Lock file (`/tmp/estonia_booker_daemon_244.lock`) confirmed removed after exit. No trigger file existed during this run, so no seat acquisition, slot polling, date refresh, or rate-limiter activity was exercised end-to-end here - this smoke test only confirms the daemon still starts, idles, and shuts down cleanly with the new scheduling code present. **T2-T9 (anything requiring a live seat) remain not run, for the same reason as round 1 (§0/§5) plus this round's explicit "make no request to the live site" constraint.**

### Anything unsure about

1. **The exact steady-state request rate in practice will depend on how many dates a real seat sees.** The arithmetic above (§ "Before/after") assumes 1 date, matching every real New Delhi trigger on record - but Abu Dhabi/Cairo/Astana (the sandboxed embassies the spec's own test plan §9 says to use for live testing) could plausibly show more than one date at once, which raises the slot-polling side of the estimate linearly (`N/2` req/s instead of `0.5`). Worth logging/checking `len(seat.available_dates)` during the first live test to confirm the real number and re-derive the estimate if it's not 1.
2. **`maybe_refresh_dates()`'s suspect check uses set equality** (`set(new_dates) == set(acquisition_dates)`) rather than exact list/order equality - this treats `["2026-08-07", "2026-08-11"]` and `["2026-08-11", "2026-08-07"]` as identical (correct, order shouldn't matter for "did anything change"), but I haven't verified whether the site could ever plausibly reorder dates without actually changing the set - if it does, that's still handled correctly by set comparison, so this seems safe, but noting the assumption explicitly.
3. **Fix 5's reacquisition happens synchronously inside `maybe_refresh_dates()`**, called from the main loop's trigger-handling branch - meaning a suspect detection blocks the main loop (and therefore R7's 0.5s trigger-polling cadence) for the full ~15-25s of a fresh CAPTCHA solve during `seat_mgr.acquire()`. This mirrors how `seat_mgr.acquire()` is already called synchronously from the main loop in the original (round 1) "no live seat yet" branch, so it's consistent with existing behavior, not a new pattern - but it does mean the daemon can't notice a *second* new trigger file update, or run `check_ttl_expiry()`/`maybe_renew()`/the balance check, while a Fix-5 reacquisition is in flight. Given reacquisitions (forced or otherwise) are rare relative to the 0.5s tick rate, this seems like an acceptable trade-off consistent with round 1's existing design, but it's worth flagging since Fix 5 is new and could increase how often this blocking happens (any new trigger while a seat looks suspect, not just "no seat at all").
4. **R17 itself is still unverified against the live site** (round 1's biggest open item, IMPLEMENTATION_CHANGELOG.md §4/§7) - Fix 5 makes a *wrong* refresh mechanism self-detecting and self-healing (which is real, meaningful progress), but it does not confirm whether `resolve_r17_refresh_dates()`'s step-4-replay approach is the *right* one to begin with. If the live site's step-4 replay turns out to never update the date list at all (not even when new dates genuinely appear), Fix 5 would fire a `DATE_REFRESH_SUSPECT` + reacquire on essentially every second-trigger-while-parked event - which is safe (it just costs a CAPTCHA and gets a fresh seat with correct dates) but would be worth noticing as a pattern in `daemon.log` during the first live test, since frequent `DATE_REFRESH_SUSPECT` lines would be a strong hint that R17's underlying mechanism needs the plain-GET-plus-steps-1-2 fallback described in round 1's §4, not just that this round's detection is working.

## 9. Follow-up fixes (round 3)

Implemented by: Claude (Sonnet 5, Cowork session) &nbsp;&nbsp; Date: 2026-08-02 &nbsp;&nbsp; Branch: `gcp-booker` &nbsp;&nbsp; Commits: *not committed — same sandbox `.git/index.lock` permission restriction as rounds 1-2; all changes are in the working tree.*

**The bug this round fixes.** Round 2's Fix 5 was backwards. Per F9/F10, a real availability episode shows the *same single date* continuously for the whole 90-110s episode (and often across episodes too) - that is the normal, healthy case, not a symptom of anything wrong. Fix 5 treated "refreshed date list identical to the list captured at acquisition time" as the SUSPECT condition. Combined with the monitor hook writing a new trigger file on *every* ~10s detection tick during an episode (not once per episode), every single one of those ~9-11 re-announcements forced an immediate refresh, found the (correctly) unchanged single date, declared it SUSPECT, dropped the seat, and blocked the main loop for a fresh ~15-25s CAPTCHA solve - consuming the entire episode in reacquisition churn and never leaving a stable window in which R15's actual slot-poll loop could run and book anything. Same sandbox constraints as rounds 1-2 apply: no network egress to `broneering.mfa.ee`, no `CAPSOLVER_API_KEY`, no Telegram credentials - nothing below required or made a live request.

### Fix table

| Fix | Description | Status | File:line | Notes |
|-----|--------------|--------|-----------|-------|
| 7 | Delete the `set(new_dates) == set(acquisition_dates)` suspect test; replace with a poll-evidence-based condition: consecutive empty-across-all-dates poll cycles while a trigger is fresh | Done | `estonia_booker_daemon.py:1020` (`check_suspect_and_maybe_reacquire`, new function), counter maintained in `poll_cycle()` `:1141` (`any_slots_this_cycle` init), `:1164` (reset to 0 on any non-empty date), `:1177` (increment when the whole cycle came up empty) | New config: `TRIGGER_FRESH_WINDOW_SECONDS` (`:98`, default 60), `SUSPECT_AFTER_EMPTY_POLLS` (`:103`, default 5). `seat_mgr.last_trigger_ts` is now set unconditionally on every `TRIGGER_RECEIVED` in `main()` (`:1251`), independent of whether a seat is live yet, so freshness can be judged the moment a seat exists. An empty *refreshed date list* while a trigger is fresh flows into this the same way an empty *poll* does, since `poll_cycle()` iterates whatever `seat.available_dates` currently holds - no special-casing needed, per the instruction that it's "the same contradiction." |
| 8 | `FORCED_REFRESH_MIN_INTERVAL_SECONDS` (new, default 30) debounces the forced-refresh path so repeat trigger re-announcements of the same episode don't each cost a request/decision | Done | `estonia_booker_daemon.py:93` (config), `maybe_refresh_dates()` `:947` (forced branch now gates on `seat_mgr.last_forced_refresh_ts`) | The independent periodic `DATE_REFRESH_INTERVAL_SECONDS` cadence (round 2, Fix 2) is untouched - Fix 8 only debounces the *forced* (trigger-driven) path, not the periodic one. |
| 9 | Suspect-driven reacquisition routes through the same overlapping-renewal machinery R11 already uses, instead of `drop_seat()` then `acquire()` synchronously | Done | `SeatManager.maybe_renew()` `:710` (now just guard-checks + calls the generalized method), `SeatManager.begin_overlapping_reacquire(reason: str)` `:723` (new, generalizes the old renewal-only `_do_renew`, shared `_renewal_lock`/`_renewal_in_progress` per R14), called from `check_suspect_and_maybe_reacquire()` with `reason="suspect"` | The existing seat keeps serving polls for the entire acquisition of its replacement; only a single reference swap happens at the end, after the replacement is verified to have reached the calendar (same "verify" semantics as R11). `reason` is currently used only for logging/telemetry context, not branching logic - both callers (`"ttl_renewal"`, `"suspect"`) go through identical code. |
| 10 | `REACQUIRE_COOLDOWN_SECONDS` (new, default 300) hard-caps suspect-driven reacquisition to at most once per cooldown window, regardless of cause | Done | `estonia_booker_daemon.py:109` (config), `check_suspect_and_maybe_reacquire()` `:1020` (cooldown check before calling `begin_overlapping_reacquire`), `SeatManager.__init__`'s `last_reacquire_ts` (documented as daemon-wide, NOT reset by `drop_seat()`/`acquire()`/renewal - only advanced by an actual reacquisition) | When suppressed, logs `REACQUIRE_SUPPRESSED` with the remaining cooldown seconds and resets `empty_poll_streak` to 0 (see "Anything unsure about" below for why that specific choice was made and what it trades off). |

### Worked example: 100-second episode, trigger every 10s, one available date

Assumptions matching the task's own framing: the monitor hook fires a new trigger file write every 10s (`t=0,10,20,...,90` - 10 events), one date is available and stays available for the whole episode (the normal case per F9/F10), `SLOT_POLL_INTERVAL_SECONDS=2` (round 2 default), and defaults for all round-3 constants (`FORCED_REFRESH_MIN_INTERVAL_SECONDS=30`, `TRIGGER_FRESH_WINDOW_SECONDS=60`, `SUSPECT_AFTER_EMPTY_POLLS=5`, `REACQUIRE_COOLDOWN_SECONDS=300`).

**Before (round 2, the bug):**

```
t=0        first trigger, no seat yet -> seat_mgr.acquire() synchronous, ~20s CAPTCHA solve   [CAPTCHA #1]
t=10..90   9 more trigger writes, one per 10s tick, each hits the (undebounced) forced-refresh
           path while a seat is live -> refreshed dates = the same single date as at
           acquisition (correct, per F9/F10) -> misclassified SUSPECT every time
           -> drop_seat() + acquire() synchronously, ~15-25s CAPTCHA each time, main loop
           blocked for the duration, seat coverage gap during every drop-to-reacquire window
```

Because each reacquisition (~15-25s) takes longer than the 10s gap between trigger writes, the daemon spends the entire 100s alternating between "blocked in a CAPTCHA solve" and "just long enough to notice the next trigger has already arrived and start another one." Per the task's own characterization of this bug: **~9-11 forced-refresh attempts, ~9-11 `DATE_REFRESH_SUSPECT` firings, ~9-11 reacquisitions, ~9-11 CAPTCHAs spent** for a single 100s episode - and R15's 2s-cadence slot-poll loop never gets a multi-cycle stretch of stable seat time in which to actually find and book the slot that was there the whole time.

**After (round 3, Fixes 7-10):**

```
t=0        first trigger, no seat yet -> synchronous acquire(), ~20s CAPTCHA   [CAPTCHA #1, unchanged - Fix 9
           only changes the *suspect* reacquisition path, not the very first acquisition]
t=10..90   9 more trigger writes -> each updates last_trigger_ts (freshness signal) but no
           longer forces a suspect decision by itself. The forced-refresh path itself is now
           debounced to at most once per FORCED_REFRESH_MIN_INTERVAL_SECONDS=30s, so of the 9
           remaining ticks roughly 2-3 actually re-run the forced refresh HTTP call
           (100s / 30s), not 9. None of these decide suspect/not-suspect any more - that
           moved entirely into check_suspect_and_maybe_reacquire(), driven by poll results.
throughout poll_cycle() runs on the independent SLOT_POLL_INTERVAL_SECONDS=2s cadence (~50
           polls across the episode) and finds the one available date's slots non-empty on
           essentially every poll, since the date genuinely is available the whole time ->
           empty_poll_streak resets to 0 on (almost) every cycle -> SUSPECT_AFTER_EMPTY_POLLS=5
           consecutive empty cycles is never reached in the true-positive case
           -> DATE_REFRESH_SUSPECT firings = 0, reacquisitions = 0, extra CAPTCHAs = 0
```

Net for the true-positive 100s episode: **1 CAPTCHA total** (the initial acquisition), **0** suspect firings, **0** reacquisitions - versus round 2's **~9-11** of each. The daemon instead spends the 100s doing what R15/R19 actually ask for: polling every 2s and booking on the first cycle that sees a slot.

**Adversarial case, for completeness** (R17's refresh mechanism genuinely is broken and slots really do come back empty for 5+ consecutive polls while a trigger stays fresh - the exact failure this machinery exists to catch): `check_suspect_and_maybe_reacquire()` fires once, `begin_overlapping_reacquire(reason="suspect")` runs (1 CAPTCHA, non-blocking to the existing seat per Fix 9), and `REACQUIRE_COOLDOWN_SECONDS=300` caps any *further* reacquisition attempts for the next 5 minutes regardless of how many more empty-poll streaks or fresh triggers occur in that window - since the episode itself is only ~100s, that's **at most 1 CAPTCHA** in the worst realistic case, versus round 2's ~9-11 for the same underlying symptom.

### Test results

**Compile check:**
```
$ python3 -c "import py_compile; py_compile.compile('estonia_booker_daemon.py', doraise=True); print('OK')"
OK
```

**`estonia_visa_monitor.py` untouched (constraint check):**
```
$ git diff --stat estonia_visa_monitor.py
$
```
Empty output - confirmed no diff. This file was not opened for editing at any point in this round-3 session either.

**`test_daemon_units.py` (extended this round with Fix 8's debounce tests and a full new suite for `check_suspect_and_maybe_reacquire`/Fix 9/Fix 10) - full real output:**

```
[PASS] rank_dates: latest >=3-day-out date ranked first
[PASS] rank_dates: all 4 input dates present in output (no silent drops)
[PASS] rank_dates: fallback to earliest-first when no date is >=3 days out
[PASS] rank_dates: R22 full ordering - good dates (>=3d out) descending, then fallback ascending
[PASS] rank_dates: malformed date strings are silently skipped
[PASS] choose_time: prefers earliest slot in 12:00-14:00
[PASS] choose_time: falls back to earliest overall when nothing is in 12:00-14:00
[PASS] choose_time: returns None for an empty list
[PASS] choose_time: skips unparseable labels rather than crashing
[PASS] extract_token: finds a plain <input> token
[PASS] extract_token: returns None when absent
[PASS] extract_token: finds a token nested inside a turbo-stream/template
[PASS] is_seat_dead: a response with both token AND date field is ALIVE
[PASS] is_seat_dead: token present but date field gone => DEAD (R13 signature)
[PASS] is_seat_dead: checkboxes present (even without the date input) is NOT treated as dead
[PASS] is_seat_dead: no token at all is NOT the R13 signature (that's a different failure - markup change)
[PASS] RateLimiter: burst allowance grants the first `burst` acquires with zero waiting
[PASS] RateLimiter: 4th acquire (bucket exhausted) waits exactly 1/rate=1.0s and advances the clock accordingly
[PASS] RateLimiter.measured_rate(): reports ~4 req in ~1s after the sequence above
[PASS] RateLimiter: RATE_THROTTLED logs exactly once for a burst of throttled acquires within one 30s log window
[PASS] RateLimiter: RATE_THROTTLED logs again once >=30s has passed since the last log
[PASS] RateLimiter: 10 sequential acquires against a slow rate all complete (blocks, never drops/raises)
[PASS] maybe_refresh_dates: not forced, interval not yet elapsed -> skipped, no refresh call, returns False
[PASS] maybe_refresh_dates: not forced, interval elapsed -> refresh IS called, returns False (nothing suspect for non-forced)
[PASS] maybe_refresh_dates: forced, but within FORCED_REFRESH_MIN_INTERVAL_SECONDS of the last forced refresh -> debounced, no call
[PASS] maybe_refresh_dates: forced, debounce interval elapsed -> refresh IS called this time
[PASS] maybe_refresh_dates: forced refresh never sets last_reacquire_ts - that belongs solely to check_suspect_and_maybe_reacquire
[PASS] SeatManager: starts with last_slot_poll_ts=0.0 (so the very first main-loop tick after acquire polls immediately)
[PASS] SeatManager.drop_seat(): resets last_slot_poll_ts/last_date_refresh_ts/dates_at_acquisition/empty_poll_streak
[PASS] check_suspect_and_maybe_reacquire: below SUSPECT_AFTER_EMPTY_POLLS -> no action, streak untouched
[PASS] check_suspect_and_maybe_reacquire: at threshold + fresh trigger + cooldown elapsed -> begin_overlapping_reacquire('suspect') called, streak reset to 0, last_reacquire_ts updated
[PASS] check_suspect_and_maybe_reacquire: stale trigger (age > TRIGGER_FRESH_WINDOW_SECONDS) -> ignored even at/above threshold, streak untouched
[PASS] check_suspect_and_maybe_reacquire: a non-empty poll resets empty_poll_streak to 0 (simulated via poll_cycle's own reset behavior)
[PASS] check_suspect_and_maybe_reacquire: within REACQUIRE_COOLDOWN_SECONDS of the last reacquisition -> REACQUIRE_SUPPRESSED logged exactly once, begin_overlapping_reacquire NOT called, streak reset, last_reacquire_ts unchanged
[PASS] check_suspect_and_maybe_reacquire: suspect reacquisition calls begin_overlapping_reacquire while has_live_seat() is still True (never drops first)
[PASS] SeatManager.begin_overlapping_reacquire: returns False if a renewal/reacquisition is already in progress (R14 concurrency cap, shared between ttl_renewal and suspect)

ALL TESTS PASSED
```

(32 checks total, 32 passed. One failure on first write, fixed before this final run: the `drop_seat()` test asserted `empty_poll_streak == 0` but `drop_seat()` hadn't actually been updated to reset it yet - added `self.last_forced_refresh_ts = 0.0` and `self.empty_poll_streak = 0` to `drop_seat()`, with a comment noting `last_reacquire_ts` is deliberately NOT reset there since it's a daemon-wide cooldown, not a per-seat one. Re-ran and all 32 passed. Recorded here per this codebase's established practice of showing real, including initially-wrong, results rather than only the clean final run.)

**Smoke test (confirms the daemon still starts/shuts down cleanly with the new logic present):**
```
$ EMBASSY_ID=244 EMBASSY_NAME="Abu Dhabi" DAEMON_DRY_RUN=1 timeout 4 python3 estonia_booker_daemon.py
```
Clean start/shutdown log lines, lock file removed after exit - same as rounds 1-2's smoke tests. Run twice, both clean. No trigger file existed, so (as in rounds 1-2) this does not exercise seat acquisition, polling, or the new suspect logic end-to-end - that remains dependent on a live test.

### Deviations from these instructions, and why

No deviations from the four fixes as specified - Fix 7's condition, Fix 8's debounce, Fix 9's routing through the shared overlapping-renewal path, and Fix 10's cooldown were all implemented literally as described. Two judgment calls were made where the instructions didn't specify an exact mechanic:

1. **`begin_overlapping_reacquire()` is a generalization of the old `_do_renew()`, parameterized by a `reason: str` used only for logging**, not for any behavioral branching between the TTL-renewal and suspect causes. The instruction said "generalise it - do not duplicate it," which this satisfies, but it was a judgment call that the two causes should behave *identically* once triggered (same verify-then-swap semantics, same `_renewal_lock`/`_renewal_in_progress` guard) rather than, say, suspect-driven reacquisition skipping the `check_service_offered()` gate that TTL renewal uses (R12). Reusing that gate for "suspect" as well seemed correct (if the D-visa service has been withdrawn entirely, mid-episode is not a good time to try re-acquiring either), but it's an inference, not something the task stated explicitly.
2. **`REACQUIRE_SUPPRESSED` resets `empty_poll_streak` to 0** rather than leaving it at/above threshold. This was a judgment call to avoid re-logging `REACQUIRE_SUPPRESSED` on every single subsequent poll cycle for the remainder of the cooldown window (which would otherwise happen every 2s once the streak is already ≥5 and staying there) - see "anything unsure about" below for the trade-off this introduces.

### Anything unsure about

1. **Resetting `empty_poll_streak` on `REACQUIRE_SUPPRESSED` could mask a persistent problem recurring within one cooldown window.** If R17's refresh mechanism is genuinely broken (round 1 §4/§7's still-open item) and stays broken for the full 300s `REACQUIRE_COOLDOWN_SECONDS`, the counter has to climb back up to `SUSPECT_AFTER_EMPTY_POLLS=5` from zero each time before `REACQUIRE_SUPPRESSED` logs again, rather than logging continuously. This means `daemon.log` would show `REACQUIRE_SUPPRESSED` roughly once per `5 × SLOT_POLL_INTERVAL_SECONDS` (~10s) during a genuinely-broken stretch, not once per poll - which seems like reasonable log hygiene, but it does mean the log undercounts exactly how bad the underlying breakage is during the cooldown. An operator watching for R17 health should look at frequency of `DATE_REFRESH_SUSPECT`/`REACQUIRE_SUPPRESSED` pairs across cooldown windows, not raw poll-level detail.
2. **The defaults (`SUSPECT_AFTER_EMPTY_POLLS=5` at 2s cadence ≈ 10s, `TRIGGER_FRESH_WINDOW_SECONDS=60`, `REACQUIRE_COOLDOWN_SECONDS=300`) are reasoned from the spec's documented episode length (90-110s) and date persistence (F9/F10), not verified against a live episode** - this whole mechanism inherits round 1's biggest open item (R17 itself unverified against the live site, §4/§7) as its foundation. If R17's actual live behavior turns out to refresh dates correctly, this machinery should rarely if ever fire in production, which would be the sign everything is working; frequent `DATE_REFRESH_SUSPECT` firings on the first live test would point back at R17, not at these thresholds, and should be investigated there first per §4's original guidance.
3. **The worked example above assumes exactly one available date and a perfectly regular 10s trigger cadence** - both match the spec's own documented New Delhi/episode behavior, but a live episode with more dates or an irregular re-announcement cadence would change the precise before/after numbers (though not the qualitative conclusion: round 2's mechanism scaled with trigger count regardless of actual availability, round 3's scales with actual poll evidence).
