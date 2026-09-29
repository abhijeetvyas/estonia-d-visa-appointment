# AI agent context: Estonia D-Visa Slot Monitor

This document exists specifically to give another LLM/agent full context to continue
development on this repo without re-deriving decisions already made. It is written for
machine ingestion, not just human reading — it is intentionally exhaustive and explicit
rather than concise. If you are an AI agent picking this project up, read this entire file
before making any changes.

Repo: `https://github.com/abhijeetvyas/estonia-d-visa-appointment`
Human owner: Abhijeet Vyas.
Live branch: **`main`** (renamed from `gcp-daemon` on 2026-08-22 — see the update note below) is the current branch to work from — see `README.md`'s
Booker/daemon section (originally a separate `BOOKER_README.md`, merged into the main README once
the repo went public). `gcp-setup` was the original detection-only branch, and
`gcp-booker` came next — it added a Playwright-driven auto-booker on top of the original monitor
(see section 8 for its full reasoning trail) — before development continued on to `gcp-daemon`.
As of the `gcp-booker` era described in sections 9-10 below, `gcp-booker` had been extensively
validated via `DRY_RUN=1` dry-runs on the VM but confirm with the human whether `run_monitor.sh`
has actually been pointed at `estonia_visa_monitor_booker_hook.py` (live) versus still calling
`estonia_visa_monitor.py` directly (detection-only) before assuming which mode is currently live
in production.
**2026-08-22 update: this repo now has a single branch.** The branch described throughout
this file as `gcp-daemon` was renamed to `main`, and the separate `only-monitor` branch (the
GitHub-Actions-only version this paragraph used to warn about) was deleted once `gcp-daemon`'s
content fully superseded it. Any reference below to "the `gcp-daemon` branch" now means `main`;
there is no other branch to check against.

---

## 1. What this system does, in one paragraph

Checks `https://broneering.mfa.ee/en/` (Estonia's Ministry of Foreign Affairs consular
booking portal) on a recurring schedule, currently every 10 seconds, to detect the moment the
service option `"Long-stay visa (D-visa) application"` becomes selectable in the booking
form's service dropdown for a specific embassy (currently New Delhi, `esindus` value `40`).
When found, it sends a Telegram message to one or more recipients and places a phone call via
Twilio. It also alerts (Telegram only, not calls) when the check itself fails to run
correctly, with guardrails to avoid alert spam.

## 2. Why this exists and the reasoning trail (read this before changing architecture)

The human originally had this running as an agentic Gumloop flow, where an LLM interpreted
the script's output on every scheduled run. That was abandoned specifically because the
underlying task is fully deterministic (fetch a page, check for a literal string, alert if
found) and does not benefit from LLM reasoning at execution time — every scheduled run was
burning tokens for zero incremental value. **Do not suggest re-introducing an agent/LLM into
the runtime execution path of this checker.** LLM use is appropriate for *designing/debugging*
this system, not for *running* it.

The system went through this evolution, each step driven by a real, observed failure, not
speculation:

1. **Plain Python script** (`requests` + `BeautifulSoup`), no agent, no LLM at runtime.
2. **GitHub Actions `schedule` trigger**, initially every 15 minutes. Observed to drift badly
   — GitHub's scheduler is explicitly best-effort for sub-hourly schedules on low-traffic
   repos, and runs were observed firing as infrequently as every 1–1.5 hours instead of every
   15 minutes.
3. **External trigger via cron-job.org** calling GitHub's `workflow_dispatch` REST API on a
   precise schedule, to work around GitHub's unreliable internal scheduler. This was a
   stepping-stone fix, since GitHub Actions runners still had the network-layer problem below.
4. **Network-layer failures on GitHub Actions**: intermittent `ConnectTimeout` and later
   `SSLCertVerificationError` against the target site. Hypothesis was IP-range-based blocking
   of GitHub Actions' shared Azure IP pool.
5. **Moved to a dedicated GCP `e2-micro` VM** (Always Free tier) specifically to get a stable,
   dedicated IP and to run a real OS-level `cron` instead of any cloud scheduler. The SSL error
   persisted even from this fresh, dedicated IP — which **disproved** the IP-blocking
   hypothesis and pointed to a different root cause (see next point). This is an important
   piece of evidence: don't re-litigate "maybe it's IP blocking" without accounting for this.
6. **Root cause of the SSL error**: the target server does not send its full TLS certificate
   chain. It sends only its leaf certificate, issued by Let's Encrypt's `YR2` intermediate,
   and omits that intermediate from the handshake. Browsers silently patch this via automatic
   AIA (Authority Information Access) chasing; Python's `requests`/`urllib3` does not do this
   by default. Confirmed via:
   ```
   openssl s_client -connect broneering.mfa.ee:443 -servername broneering.mfa.ee -showcerts
   ```
   which showed exactly one certificate in the chain instead of the expected leaf+intermediate.
   Fix: fetched the missing intermediate from the certificate's own AIA "CA Issuers" URL
   (`http://yr2.i.lencr.org/`, served as DER), converted to PEM, concatenated with `certifi`'s
   bundle into `combined_ca_bundle.pem`, and pointed `requests` at it via the
   `REQUESTS_CA_BUNDLE` environment variable (this is a `requests` built-in behavior — no code
   change was needed, only an env var). **If this error reappears, the first thing to check is
   whether Let's Encrypt rotated their intermediate away from `YR2`** — re-run the
   `openssl s_client` command above to find the current AIA URL and repeat the fetch/bundle
   steps.
7. **GCP `e2-micro` chosen over Oracle Cloud's Always Free tier** because Oracle explicitly
   reclaims Always Free instances it deems idle (95th-percentile CPU utilization under 20%
   over 7 days), which is exactly the usage profile of a cron job that wakes for a couple
   seconds every 10-30 seconds and is otherwise idle. GCP's free tier has no equivalent
   idle-reclamation policy.
8. **`run_monitor.sh` calls the venv's `python3` by absolute path directly**
   (`/home/USER/estonia-d-visa-appointment/venv/bin/python3 estonia_visa_monitor.py`) rather
   than `source venv/bin/activate; python3 ...`. This is deliberate: under cron, the
   `source`-based activation was empirically observed to intermittently fail to take effect
   (majority of runs crashing with `ModuleNotFoundError: No module named 'bs4'` in one
   diagnosed stretch — 15 out of 18 runs). Calling the interpreter by full path removes the
   dependency on shell activation state entirely and fully resolved this.
9. **Logging reliability was itself a whole debugging arc.** Early on, `monitor.log` had no
   timestamps, making any historical analysis impossible. It was also manually truncated
   (`> monitor.log`) many times during live debugging sessions, which permanently destroyed
   historical data — including, at one point, the log line for a real, genuine slot-found
   event (confirmed by the human independently receiving the real alert while the log showed
   nothing). This led to two permanent design decisions:
   - Every log line is timestamped (UTC, ISO 8601) via the `log_line()` helper.
   - A **separate, append-only file, `alerts_history.log`**, records ONLY real
     AVAILABLE/FAILURE events via `record_alert()`. This file must never be manually truncated
     — unlike `monitor.log`, which is fine to clear or let `logrotate` rotate.
10. **`flock -n` silently skips overlapping cron ticks** if a previous run is still executing.
    This was initially invisible (no log output on skip), which made cron-invocation counts
    (`grep -c CRON /var/log/syslog`) diverge significantly from logged script outcomes with no
    explanation. Fixed by appending `|| echo "$(date -Iseconds) SKIPPED: ..." >> monitor.log`
    to each crontab line, so skips are now visible and countable.
11. **Cron cannot natively schedule sub-minute intervals.** The workaround used throughout is
    N crontab lines, each `* * * * *` (fires every minute) but offset via `sleep <seconds>`,
    all sharing the same `flock` lock file. This has been scaled from 2 lines (30s cadence) to
    6 lines (10s cadence, current state) by adding `sleep 10`, `sleep 20`, `sleep 30`,
    `sleep 40`, `sleep 50` lines alongside the unsleep `:00` line. At tighter intervals, expect
    a higher proportion of `SKIPPED` entries if any single check run takes close to or longer
    than the interval between ticks.
12. **Call-frequency throttling was added deliberately** because Telegram fires on every check
    (currently every 10s) uncapped, but phone calls at that frequency would ring constantly and
    be useless/annoying. Current behavior (see section 4) caps calls to 3 per "episode," 5
    minutes apart, then a 6-hour cooldown before a fresh episode can start. This state is
    tracked in the same per-embassy JSON state file used for the failure-alert cooldown.
13. **Multi-embassy support was added via environment variables** (`EMBASSY_ID`,
    `EMBASSY_NAME`), not via forking or branching the repo, specifically so the same
    codebase/guardrails/logging can be reused for a different embassy by just changing two env
    vars in a wrapper script, rather than maintaining divergent copies. The state file path is
    namespaced per `EMBASSY_ID` so two embassies checked from clones of this repo don't share
    (and incorrectly suppress) each other's cooldowns.
14. **A CAPTCHA now appears at the "choose service" step** of the booking flow (confirmed via
    screenshot from the human, reCAPTCHA, "I'm not a robot" checkbox). This means even when the
    monitor correctly detects and alerts on availability, actual booking submission still
    requires a human to manually complete the CAPTCHA and submit within whatever window the
    slot stays open (observed once to be roughly 30 seconds before another party booked it).

## 3. Current infrastructure (as of last known state)

- **Host**: Google Cloud Compute Engine, region `us-central1`. **Originally `e2-micro`**
  (Always Free tier), **upgraded to `e2-standard-2`** (2 vCPU dedicated, 8GB RAM, non-shared-
  core) after the booker's Playwright/Chromium workload was observed exhausting `e2-micro`'s
  CPU burst credits under sustained load (confirmed directly against the GCP Monitoring CPU
  Utilization graph — visible spikes to 200%, i.e. both shared vCPUs pegged, during back-to-
  back dry-runs). This upgrade takes the VM out of Always Free eligibility and incurs real
  billing. This detail was trimmed from the public README during the v0 cleanup, so it's recorded
  here instead: if `e2-standard-2` hits a `ZONE_RESOURCE_POOL_EXHAUSTED` capacity stockout in this
  zone (a transient GCP-side issue, not a config problem — happened repeatedly in `us-central1-a`),
  the fallback machine-type order is `n2d-standard-2`, `n1-standard-2`, `n2-standard-2`,
  `t2d-standard-2`, `e2-standard-4`.
- **VM hostname**: `estonia-visa-monitor`. **Linux user**: `abhijeetvyas13`.
- **Repo location on VM**: `/home/abhijeetvyas13/estonia-d-visa-appointment`, on branch
  `gcp-booker`.
- **Python environment**: a `venv` at `venv/` inside the repo directory, with `requests`,
  `beautifulsoup4`, and `playwright` installed per `requirements.txt` (`playwright install
  chromium` also required, done separately from `pip install`).
- **Persistent Chromium**: `persistent_chromium.py` runs as a systemd service
  (`estonia-chromium.service`, enabled, `Restart=always`) keeping one headless Chromium warm
  with a local CDP port (`127.0.0.1:9222`) open — the booker attaches via
  `connect_over_cdp()` instead of paying Chromium's cold-start cost (10-30s+, worse under the
  old CPU throttling) on every triggered run. Falls back to a fresh launch automatically if
  unreachable, so this being down doesn't block real bookings, only slows them.
- **Google Cloud Ops Agent**: installed for real Memory/Disk Monitoring dashboard visibility
  (the default dashboard only shows hypervisor-level metrics without it). Has repeatedly shown
  `[API Check] Result: FAIL, MonApiPermissionErr/LogApiPermissionErr` in its own status logs —
  this is an IAM issue, not an access-scope issue: the instance's "Allow full access to all
  Cloud APIs" access scope being set is necessary but not sufficient; the VM's service account
  (the default Compute Engine one, `<project-number>-compute@developer.gserviceaccount.com`)
  also needs the `Monitoring Metric Writer` and `Logs Writer` IAM roles granted directly in
  IAM & Admin → IAM. Check this is actually done before assuming Ops Agent monitoring works.
- **Not tracked in git** (exist only on the VM, must be manually recreated if the VM is
  rebuilt): `run_monitor.sh` (contains all secrets as `export` statements), `venv/`,
  `combined_ca_bundle.pem`, `yr2_intermediate.der`/`.pem`, `.state/` (per-embassy JSON cooldown
  state, now also per-embassy booker state via `.state/booker_state_<EMBASSY_ID>.json`),
  `monitor.log` and its rotations, `alerts_history.log`, `applicant_data_real.json`,
  `booker_screenshots/` and `booker.log` (booker-specific, dry-run/debug artifacts).
- **Scheduling**: system `cron` (via `crontab`), six lines for 10-second cadence (see section
  2, point 11), each wrapped in `flock -n /tmp/estonia_visa_monitor.lock`.
- **Log rotation**: `/etc/logrotate.d/estonia-visa-monitor`, daily, 7 rotations kept,
  compressed with `delaycompress`, explicit `create 644 abhijeetvyas13 abhijeetvyas13` and
  `su abhijeetvyas13 abhijeetvyas13` directives (required because the log lives in a
  non-root-owned home directory — logrotate refuses to operate on such paths without `su`).

## 4. Script structure (`estonia_visa_monitor.py`) — function-by-function

- `check_availability()`: does the GET (fetch landing page, extract CSRF token from
  `input[name="broneering[_token]"]`), then POST (submit `broneering[esindus]=EMBASSY_ID`,
  `broneering[isikuteArv]=1`, the CSRF token), then parses the response's
  `select[name="broneering[toiming]"]` (or `#broneering_toiming`) for `<option>` values.
  Returns `(found: bool, available: list[str])`. Raises `RuntimeError` with a specific message
  if the CSRF token, form, or dropdown aren't found (treated as a possible site-markup-changed
  condition, not "no slots" — this distinction matters, don't collapse it).
- `EMBASSY_ID` / `EMBASSY_NAME`: env vars, default to New Delhi (`'40'`, `'New Delhi'`).
- `_telegram_creds()` / `send_telegram_text()`: `TELEGRAM_CHAT_ID` supports comma-separated
  multiple recipients. Sending is best-effort per recipient — only raises if ALL recipients
  fail; partial failure is logged but doesn't crash the run.
- `trigger_phone_calls(bypass_cooldown=False)`: Twilio REST API call via plain `requests` (no
  Twilio SDK dependency, kept consistent with the rest of the script's minimal-dependency
  approach), one call per number in comma-separated `TWILIO_TO_NUMBERS`. Cooldown logic:
  `CALL_COOLDOWN_SECONDS = 300` (5 min) between calls within an "episode",
  `MAX_CALLS_PER_ESCALATION = 3` calls per episode, `CALL_ESCALATION_COOLDOWN_SECONDS = 21600`
  (6h) required gap once the cap is hit before a new episode can start. State (`last_call_ts`,
  `call_count`) is persisted in the same per-embassy state file as the failure cooldown.
  `bypass_cooldown=True` is used only by the test path, so test calls always go through
  immediately regardless of real cooldown state.
  If env var `ENABLE_TWILIO_CALLS` is `0`/`false`/`no`, the function logs and returns before
  reading credentials or touching cooldown state (see section 5).
- `send_availability_alert()`: sends Telegram, then `trigger_phone_calls()`, then
  `record_alert()` to the permanent alerts log. Called only when `found=True`.
- `send_failure_alert()` / `handle_failure()`: Telegram only, never calls, by design (frequent
  expected failures during outages shouldn't ring anyone). `FAILURE_ALERT_COOLDOWN_SECONDS =
  5400` (1.5h). `clear_failure_state_if_needed()` resets this cooldown once a check succeeds
  again, so a *new* distinct failure later doesn't have to wait out an old cooldown window.
- `send_test_alert()`: triggered when env var `TEST_TELEGRAM` is `1`/`true`/`yes`. Sends a 🧪
  message and places test calls with `bypass_cooldown=True`. Does NOT touch the real site at
  all — pure alerting-path test.
- `log_line()`: prints with a UTC ISO-8601 timestamp prefix. Use this instead of bare `print()`
  for anything that should be timestamped in `monitor.log`.
- `record_alert()`: appends a timestamped line to `alerts_history.log`. Only called for real
  AVAILABLE/FAILURE events, never for routine "not available" or test events.
- `RUNTIME_SIGNATURE`: appended to every Telegram message body,
  `f"\n\n— Sent from {os.environ.get('RUNTIME_LABEL', 'GCP')}"` — the machine identifier is
  now driven by the `RUNTIME_LABEL` env var (defaults to `'GCP'`, unchanged behavior if unset)
  rather than a hardcoded string. This was made configurable specifically to support running
  the monitor/booker on multiple machines in parallel (e.g. the VM and a personal laptop
  simultaneously, both racing to catch the same slot) and being able to tell from the Telegram
  alert which one actually fired — set `RUNTIME_LABEL="Mac"` (or similar) on any non-VM runtime.
  The same env var also drives the equivalent hardcoded strings in
  `estonia_visa_monitor_booker_hook.py` and the "Check monitor.log on {X}" text in
  `send_failure_alert()` — all three were updated together, don't assume only one was.

## 5. Required environment variables (all set in `run_monitor.sh`, none are defaults except embassy)

| Variable | Purpose | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot API token | From @BotFather |
| `TELEGRAM_CHAT_ID` | Recipient chat ID(s) | Comma-separated for multiple recipients |
| `REQUESTS_CA_BUNDLE` | Path to `combined_ca_bundle.pem` | Required — see section 2.6 |
| `TWILIO_ACCOUNT_SID` | Twilio account SID | |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | |
| `TWILIO_FROM_NUMBER` | Twilio phone number placing calls | E.164 format |
| `TWILIO_TO_NUMBERS` | Recipient phone number(s) | Comma-separated, E.164 format, must be verified caller IDs if Twilio account is still on trial |
| `TWILIO_TWIML_URL` | TwiML Bin URL (what the call says) | Created in Twilio console |
| `ENABLE_TWILIO_CALLS` | Optional, defaults to enabled | Set to `0`/`false`/`no` (case-insensitive) to skip phone calls without removing Twilio credentials, e.g. while a trial account can't call certain regions. Checked at the top of `trigger_phone_calls()`, so it also silences test-run calls. Logs `Twilio calls disabled via ENABLE_TWILIO_CALLS=0` and returns. |
| `EMBASSY_ID` | Optional, defaults to `'40'` (New Delhi) | Numeric `esindus` value from the site's own dropdown |
| `EMBASSY_NAME` | Optional, defaults to `'New Delhi'` | Human-readable, used in alert text only |
| `TEST_TELEGRAM` | Optional, unset by default | Set to `true` to trigger test-mode (see `send_test_alert`) |
| `RUNTIME_LABEL` | Optional, defaults to `'GCP'` | Identifies which machine sent an alert in the Telegram text — set differently per machine if running in parallel on more than one (see section 8) |

## 6. Command reference (also in README.md, repeated here for this doc's self-containedness)

```bash
cd ~/estonia-d-visa-appointment
./run_monitor.sh                        # one real check
TEST_TELEGRAM=true ./run_monitor.sh     # test alert path, all channels, bypasses call cooldown
tail -f monitor.log                     # live log
cat alerts_history.log                  # permanent real-event record — never truncate this one
crontab -l                              # view schedule
grep -c CRON /var/log/syslog            # ground-truth count of cron invocations
sudo logrotate -f /etc/logrotate.d/estonia-visa-monitor   # force a rotation
```

Combined historical report across current + rotated + compressed logs:
```bash
ALL_LOGS=$( { cat monitor.log monitor.log.1 2>/dev/null; zcat monitor.log.*.gz 2>/dev/null; } )
echo "Total: $(echo "$ALL_LOGS" | grep -cE "NOT available|AVAILABLE:|MONITOR FAILURE")"
echo "Available: $(echo "$ALL_LOGS" | grep -c 'AVAILABLE:')"
echo "Not available: $(echo "$ALL_LOGS" | grep -c 'NOT available')"
echo "Failures: $(echo "$ALL_LOGS" | grep -c 'MONITOR FAILURE')"
echo "Skipped: $(echo "$ALL_LOGS" | grep -c 'SKIPPED')"
```

To edit crontab, always build via heredoc rather than the interactive `crontab -e` editor —
browser-based SSH terminals have repeatedly been observed to mangle multi-line pastes into
`nano`:
```bash
cat > /tmp/current_cron << 'EOF'
<lines here>
EOF
crontab /tmp/current_cron
```

## 7. Known gaps / things a future agent should NOT assume are bugs

- Historical run counts in `monitor.log`/`alerts_history.log` will always undercount true
  elapsed cron ticks by some amount, because the log was manually cleared (`> monitor.log`)
  multiple times during live debugging sessions before these guardrails existed. This is
  expected and not indicative of a current bug — always cross-check against
  `grep -c CRON /var/log/syslog` before concluding something is actually broken.
- `SKIPPED` entries in the log are normal and expected, especially at tight (10s) cron
  intervals — they mean `flock` correctly prevented an overlapping run, not that something
  failed.
- If a fresh SSL `CERTIFICATE_VERIFY_FAILED` error appears again, check Let's Encrypt's
  intermediate rotation first (section 2.6) before assuming a new, different problem.
- Two GitHub secrets flows exist historically in this project's development (repo Actions
  secrets for the abandoned GitHub-Actions-only version, and a cron-job.org-triggered PAT for
  an intermediate stepping-stone version) — neither of these is part of the current live
  system. The live system's only secrets live in `run_monitor.sh` on the GCP VM.
- The site itself can and does change its markup / add friction (the CAPTCHA in section 2.14
  is a live example). Treat `RuntimeError`s from `check_availability()` about "markup may have
  changed" as a signal to actually go look at the live site (e.g. via a real browser session)
  before assuming it's a transient bug.

## 8. The booker (`estonia_d_visa_booker.py`, `gcp-booker` branch) — full reasoning trail

Everything below happened after section 1-7 were written and describes a second system layered
on top of the original monitor. `estonia_visa_monitor.py` itself was never modified by any of
this — `estonia_visa_monitor_booker_hook.py` is the only file that imports it and, when a slot
is found and `ENABLE_BOOKER=1`, launches the booker as a separate detached subprocess (so a
booker crash can't take down the monitor's own 10-second cadence).

**Why it exists**: detection alone wasn't enough — the first real slot the monitor caught was
lost anyway, because by the time a human opened a laptop and clicked through the form, someone
else had already taken it (one observed window: roughly 30 seconds). The booker exists to remove
the human from that specific race.

**Three-stage flow**: (1) detection, unchanged, handled by the original monitor; (2) reach the
calendar, collect every available date/time, send a Telegram listing them — runs every time the
booker is triggered, even if already booked; (3) select date/time per preference rules
(`choose_date()`: latest date ≥3 days out, fallback earliest; `choose_time()`: earliest slot in
12:00-14:00, fallback earliest overall), solve the second CAPTCHA, submit — skipped permanently
once a persistent `booked` flag is set in `.state/booker_state_<EMBASSY_ID>.json`.

**Bug: date/time selection silently returning empty, every single run, regardless of embassy or
how many dates were available.** Root cause was two independent, compounding issues, both in the
calendar-day-click code, neither of which looked anything like a "reading the wrong attribute"
bug (which was the first, reasonable-but-incomplete hypothesis):
1. The code compared flatpickr's rendered `aria-label` (a locale-formatted string like
   `"August 18, 2026"`) against an ISO date string (`"2026-08-18"`) — a comparison that could
   never once succeed, so the day-click silently never fired. No exception, no timeout, just a
   no-op. Fixed by matching on the day-of-month number within the currently-rendered month
   instead (locale-independent) — flatpickr already auto-navigates to the correct month.
2. A second, separate piece of code was manually setting the hidden `broneering[kuupaev]` input
   to the raw ISO string and dispatching generic `change`/`input` DOM events on it. The
   Stimulus/Turbo controller backing this form expects `DD.MM.YYYY` and drives off flatpickr's
   own `onChange` — feeding it a malformed value this way was actively resetting/reloading the
   form, wiping out whatever slots the (now-fixed) day-click had just correctly loaded. Fixed by
   driving flatpickr's own `input._flatpickr.setDate(dateStr, true)` API instead, which handles
   formatting and fires the real internal `onChange` the Stimulus controller is listening for.
   Falls back to the day-of-month click if `_flatpickr` isn't found on the input (observed to
   happen in practice on the live VM, harmlessly — the fallback works fine).
   **A second, independently-run AI agent found bug #2 (the Stimulus/Turbo reset) while missing
   bug #1 (the aria-label mismatch) that had already been found and fixed here.** Neither
   diagnosis was wrong; neither was complete alone. If debugging this class of "reached the
   right state but data came back empty" bug again, don't assume a single diagnosis (from any
   source, human or AI) is the whole story — cross-check independently before declaring it fixed.
   Also hardened while here: time-slot extraction now pairs each checkbox with its real label via
   `label[for=checkbox.id]` (not DOM-proximity guessing or a page-wide div/span text scan), and
   selects the target slot by its exact value (internal slot ID, e.g. `"2249050"` — NOT the
   displayed time, a separate thing worth knowing if this code is touched again) instead of fuzzy
   text matching.

**CAPTCHA solving, provider history**: originally 2captcha only (`TWOCAPTCHA_API_KEY`,
`_solve_recaptcha_2captcha()`, observed solve times 21-67s). Switched to **CapSolver as the
default** (`CAPSOLVER_API_KEY`, `_solve_recaptcha_capsolver()`, observed solve times mostly
12-40s, i.e. usually faster but with real variance) after direct comparison. Both
implementations were kept, not replaced — `CAPTCHA_PROVIDER` env var selects `capsolver`
(default), `2captcha`, or `race` (see below). The two APIs are NOT compatible drop-ins for each
other (different endpoints, request/response shapes, polling semantics) — this is why there are
two separate `_solve_recaptcha_*` functions with `solve_recaptcha()` as a thin dispatcher, not
one function with an if/else on request format.

**`CAPTCHA_PROVIDER=race`**: fires both CapSolver and 2captcha concurrently for the same
challenge, keeps whichever result arrives first, discards the other. Added deliberately despite
doubling cost on nearly every real attempt (both are pay-per-successful-solve, not pay-per-
request, and neither has a cancel-in-flight-task API, so the losing solve still completes and
still gets billed) — accepted because this only runs at real trigger time (rare slot-detection
events), not continuously, so the extra cost is trivial in absolute terms. **Do not confuse this
with, or reintroduce, a continuously-refreshed CAPTCHA-token "reserve"** (an idea considered and
explicitly rejected) — since these services bill per successful solve regardless of whether the
resulting token is ever used, continuously pre-solving on a timer (independent of whether a real
slot has actually appeared) would be a real, ongoing cost for zero benefit over the
already-in-place background-prefetch-at-trigger-time approach, and most pre-solved tokens would
go stale anyway (reCAPTCHA v2 tokens are valid roughly 2 minutes) before a real, randomly-timed
trigger event ever aligned with them.

**Background CAPTCHA prefetching**: both `solve_recaptcha()` calls (one per CAPTCHA in the flow)
are now preceded by kicking off background solves for both CAPTCHAs at the very start of
`run_booker()`, before the browser even launches — via `_prefetch_captcha_worker()` and two
daemon threads, using the hardcoded `RECAPTCHA_SITEKEY` constant and known `URL` (neither depend
on any page state). `get_recaptcha_token()` compares the page's actual extracted sitekey against
what was prefetched; if they match, it just waits for/reuses the in-flight solve; if they don't
(e.g. the hardcoded constant went stale), it falls back to a fresh synchronous solve exactly like
before — **safe by construction, correctness never depends on the prefetch working, only speed
does.** This is the single biggest speed win in the whole booker: CAPTCHA #2 in particular isn't
needed until 50-90s into a typical run, by which point its background solve is very often already
done, removing its entire wait from the critical path. Measured effect: a full dry-run went from
the 90-130s range down to as little as 31s in one run (though NOT reliably every run — a
41s run showed each CAPTCHA still causing a partial synchronous wait when that particular solve
happened to be slower than the page-navigation time available to hide behind it; this variance is
exactly what `CAPTCHA_PROVIDER=race` is meant to reduce). **Known unhardened edge case**: the
prefetch result isn't checked for staleness/age before use — if a run were ever unusually slow
(approaching the ~2-minute reCAPTCHA token validity window) before reaching CAPTCHA #2, the
prefetched token could theoretically have expired; this hasn't happened in practice (runs are
well under that window) but would be worth adding a staleness check for if it ever does.

**Persistent Chromium (`persistent_chromium.py`)**: see section 3. Attach-or-fallback logic lives
in `run_booker()`'s browser-acquisition block — `connect_over_cdp()` first, fresh
`p.chromium.launch()` on any failure. The `finally` block only calls `browser.close()` if a fresh
browser was launched (`used_persistent_browser` flag) — closing a shared, persistent instance
here would kill it for every future run relying on it, not just this one.

**Local/laptop-specific env vars** (all opt-in, default off, VM/live behavior completely
unaffected by any of them): `BROWSER_CHANNEL=chrome` (real installed Chrome instead of bundled
Chromium), `BROWSER_HEADLESS=0` (visible window), `BROWSER_USER_DATA_DIR=<path>` (persist
cookies/history/local storage across runs via `launch_persistent_context()` — mutually exclusive
with the persistent-Chromium-over-CDP path, takes priority if set, since it's solving a different
problem: profile/fingerprint continuity, not launch-speed). These exist to test an unconfirmed
hypothesis — that a real Chrome browser on a real residential IP with real browsing history gets
easier reCAPTCHA challenges than a headless instance from a GCP datacenter IP, since reCAPTCHA
risk-scores on signals like IP reputation and fingerprint. **This has not been confirmed or
disproven yet** — treat it as a hypothesis worth testing (compare CAPTCHA difficulty/frequency
between a VM run and a warmed-up-profile local run), not an established fact. When
`BROWSER_CHANNEL` is set, the context is deliberately NOT given the hardcoded fake `user_agent`
string that the plain-Chromium fallback path uses — overriding a real Chrome's UA risks a
version/platform mismatch that's itself a detectable inconsistency; let the real browser report
its own true identity.

**Running in parallel on multiple machines**: the VM and a personal laptop can both run the full
monitor+booker pipeline simultaneously, watching/racing independently — explicitly decided as
acceptable despite there being **no shared state between machines**. Each machine has its own
`.state/` directory, so if both detect the same slot and both attempt to book it, there is a real
possibility of both submitting near-simultaneously with no coordination preventing it. This was a
deliberate, informed choice (not an oversight) made when weighing redundancy against that risk —
if this behavior is ever revisited, that trade-off was made knowingly, not missed.
`RUNTIME_LABEL` (section 5) exists specifically so alerts are distinguishable by source machine
when running this way.

## 9. Aug 2026 session: contributor regression fix + real production log analysis (read this before touching monitor.py, the workflow YAML, or trying to speed up detection frequency)

This section documents a single analysis/debugging session by an AI agent (Claude, via Cowork)
on 2026-08-02, covering three things a future agent (including a fresh Claude/Opus/Fable session
picking this up) needs to know before re-doing any of this work: (1) a set of real bugs found and
fixed on `gcp-booker`, (2) real measured production data (not estimates) on execution time and
polling frequency, and (3) what is already optimized in the booker so effort isn't wasted
re-suggesting it. **As of this writing, the fix in (1) exists only as a local commit on the
machine that has this repo synced — it has NOT been pushed to `origin/gcp-booker` and has NOT
been pulled onto the GCP VM.** See "Deploy status" at the end of this section before assuming
either the fix or the VM's current running code is in any particular state.

### 9.1 Bugs found in commits `615bbb0`, `ff476e7`, `d108749` (all by a contributor, all on `gcp-booker` only — `main` and `gcp-setup` never had these commits, confirmed via `git merge-base --is-ancestor`, so those two branches needed no fix)

These three commits (adding `lxml`, switching the monitor to session-reuse + `lxml` parsing +
a retry loop, and adding pip caching to the GitHub Actions workflow) were well-intentioned but
left the branch in a state that does not even compile. Confirmed directly (not assumed) via
`python3 -c "import py_compile; py_compile.compile('estonia_visa_monitor.py', doraise=True)"` and
`python3 -c "import yaml; yaml.safe_load(open('.github/workflows/estonia-visa-monitor.yml'))"` —
both failed before the fix, both pass after. Four fixes were applied, all in
`estonia_visa_monitor.py` and `.github/workflows/estonia-visa-monitor.yml`, committed together as
`5dc496b` on `gcp-booker`:

1. **`.github/workflows/estonia-visa-monitor.yml`**: `cache:'pip'` (missing space after the
   colon) is invalid YAML — a mapping key needs `cache: 'pip'`. Confirmed via PyYAML raising
   `could not find expected ':'`. This only affects the GitHub Actions fallback path (section 1),
   not the live VM, but would break that workflow entirely if it were ever exercised (e.g. via
   `TEST_TELEGRAM` workflow_dispatch or if the VM cron ever goes down and this is used as a
   backup). Fixed: added the space.
2. **`get_session()` in `estonia_visa_monitor.py`**: the `return _session` line was indented
   *inside* the `if _session is None:` block, not after it. This means the very first call in a
   process creates and returns a session correctly, but every subsequent call to `get_session()`
   in the same process — e.g. `send_telegram_text()` or `trigger_phone_calls()` calling it after
   `check_availability()` already created one — falls through and returns `None` implicitly.
   Confirmed by executing the exact function body directly: first call returned the real session
   object, second call returned `None`. This would have crashed with
   `AttributeError: 'NoneType' object has no attribute 'post'` at the exact moment a real
   AVAILABLE alert tries to fire — the single worst place for a silent regression in this whole
   system, since it would look like "the monitor is fine" (the check itself doesn't call
   `get_session()` twice) right up until a real slot is found and the alert path crashes instead
   of sending. **Important: this bug has never actually fired in the observed production logs**
   (see 9.2 — the 5 real slot-detection events on record all predate this commit and ran under
   whatever code was live before `gcp-booker`'s tip moved to `d108749`), so there is no evidence
   of an alert having been silently lost to this specific bug yet — but it would be live-breaking
   the next time a real slot is found, if this fix is not deployed first. Fixed: moved
   `return _session` outside the `if` block (unconditional return).
3. **Dead code in `main()`**: the old single-retry logic was "commented out" by wrapping it in a
   `'''...'''` string, but the closing `'''` was on its own line at a different indentation level
   than the opening one, which broke Python's block structure entirely (`IndentationError:
   unexpected indent` at the `max_attempts = 2` line, confirmed via `py_compile`). This is not a
   logic bug, it's a leftover editing artifact — likely from an editor auto-indent or a manual
   half-finished comment-out. Fixed: deleted the dead block entirely, kept only the new
   `for attempt in range(max_attempts)` retry loop that was clearly the intended replacement.
4. **Timeouts dropped from 30s to 10s** on all four outbound calls (site GET, site POST, Telegram
   send, Twilio call) in the same commit. Given this entire project's documented history (sections
   2.4-2.6) is about fighting slow/flaky connections to this specific site, a tighter timeout
   means a higher rate of false `MONITOR FAILURE` alerts under ordinary site slowness — actively
   working against reliability effort already invested, for no stated benefit (the commit message
   doesn't mention a timeout-related problem being solved). Fixed: reverted all four back to 30s.
   If someone deliberately wants tighter timeouts later, do it as its own explicit, justified
   change, not bundled silently into an unrelated session-reuse commit.

The `lxml` switch and the general idea of a retry loop and session reuse were kept — those are
genuinely good changes, only the execution had bugs. Don't re-revert those parts.

### 9.2 Real production measurements taken on the VM (2026-08-02, `abhijeetvyas13@estonia-visa-monitor`) — use these, not estimates from reading the code

The plain monitor's execution time was estimated from code-reading earlier in this same session
(sub-1-2s guess) and then *actually measured* directly on the VM via `time ./run_monitor.sh`,
run 5 times back to back:

```
real 0m0.972s / 0m1.011s / 0m1.021s / 0m1.082s / 0m0.978s
```

So: **~1 second flat per check**, consistently, against the 30s timeout ceiling (now correctly
restored per 9.1.4) and the 10s polling interval. This confirms the check itself uses roughly
10% of its available time budget per tick, with wide headroom before the interval itself becomes
the constraint.

Polling frequency was confirmed as genuinely sustained at ~10s, not just configured that way:
`awk '{print $1}' monitor.log | tail -500` showed timestamps landing in ~10s steps
(`02:05:42, 52, 02:06:02, 12, 22, 32, 42, 52...`) with only minor ±1-2s jitter, `grep CRON
/var/log/syslog | tail -30` confirmed cron itself firing exactly 6 times per 60s as configured,
and critically, **`tail -500 monitor.log | grep -c SKIPPED` returned `0`** — zero overlapping-run
skips in the last 500 log lines. This means the 10s cadence is currently fully sustained with no
contention at all (consistent with the ~1s execution time measured above), not degraded or
silently running slower than configured.

**The actual bottleneck, confirmed via `booker.log`, is not detection frequency — it's the
booker's own execution time relative to the real-world race window.** Every real trigger event on
record (`grep -E 'Booker starting|Booker finished' booker.log`) resulted in `no_time_slots` — the
monitor correctly detected an available date every time, but by the time the booker reached the
calendar, selected the date, and exhausted its 3 retries waiting for a bookable checkbox to
render, nothing was left:

| Run (2026-07-31) | Start → End | Duration |
|---|---|---|
| 1 | 05:57:24 → 05:58:09 | 45s |
| 2 | 06:04:24 → 06:05:00 | 36s |
| 3 | 06:30:43 → 06:31:30 | 47s |
| 4 | 06:35:33 → 06:37:13 | 100s |
| 5 | 06:40:33 → 06:41:46 | 73s |

Every single one of these is longer — some 2-3x longer — than the ~30-second real-world race
window documented in section 2.14 (how fast another applicant took a slot after it first appeared,
observed once). **These 5 events predate `gcp-booker`'s tip moving to the contributor's commits** (they
ran under whichever code was live on the VM before `d108749`/`ff476e7`/`615bbb0` existed), so they
are not evidence of the bugs in 9.1 causing this — they reflect the booker's inherent speed against
this specific race, independent of that regression.

**Conclusion, stated explicitly so a future agent doesn't have to re-derive it**: detection is not
the constraint. It runs in ~1s against a 10s interval with zero skip contention, i.e. there is
technically slack to poll tighter than 10s without breaking anything mechanically — but doing so
would not move the needle on the actual observed failure mode, which is that the booker takes
36-100s to even attempt securing a slot, well past the ~30s window competitors appear to be filling
it in. **Do not spend effort tightening the cron interval below 10s as a fix for lost bookings —
the evidence points entirely at booker-side speed (or a stale-availability-list issue, see below)
as the lever that matters.** Tightening polling further also has real downside: cron's granularity
is fixed at 1 minute so going tighter means more staggered lines, actual check runtime would start
approaching the interval at some point (though currently far from it), and more frequent requests
against a government portal risks inviting stricter anti-automation measures (a CAPTCHA already
appeared at the service-selection step per section 2.14).

**Open question worth investigating, not yet root-caused**: why has `no_time_slots` happened on
100% of recorded real trigger events (5/5)? Two different, non-exclusive hypotheses, neither
confirmed: (a) the booker genuinely is too slow end-to-end (36-100s) and slots really are gone by
the time it checks, or (b) the calendar's "available dates" list (`vastuvotukuupaevad` attribute,
see `run_booker()`'s calendar-extraction code) is itself stale/optimistic — i.e. it may list a date
as having capacity based on stale server-side state even when the actual per-slot inventory for
that date is already empty, independent of how fast the booker runs. The existing 3-attempt retry
loop with re-selection (added specifically to handle exactly this "date says available, slots come
back empty" case — see section 8 above, and the retry logic itself in the code) already assumes
hypothesis (b) is at least partially true, but 5/5 failures despite that retry loop suggests either
the retry window (3 attempts, 3s apart, ~9-15s total) still isn't enough, or something more
structural is going on. A future agent picking this up should treat this as the highest-leverage
open question, not booker-vs-monitor speed tuning in isolation.

### 9.3 Already optimized in the booker — do not re-suggest these as if they're gaps

For a future agent asked to "improve" booking speed/success rate: the following are already
implemented and reasoned through in this codebase (with the specific reasoning already documented
in section 8 above) — re-proposing any of these from scratch wastes a turn:

- Background CAPTCHA prefetching for both reCAPTCHAs, started at the very beginning of
  `run_booker()`, before the browser even launches (section 8, "Background CAPTCHA prefetching").
- `CAPTCHA_PROVIDER=race` mode — fires CapSolver and 2captcha concurrently, keeps whichever
  finishes first (section 8, `_race_solve_recaptcha`). Currently the default in
  `run_booker_dryrun.sh`, but check `run_monitor.sh` on the VM for what's actually set live (it currently sets
  `CAPTCHA_PROVIDER=race` per the crontab dump taken this session).
- Persistent warm Chromium via CDP (`persistent_chromium.py` + systemd service) to skip the
  10-30s+ cold-launch cost — falls back to fresh launch automatically if unreachable.
- Event-driven waits (`page.wait_for_selector(...)`) replacing flat sleeps between form-transition
  steps, with a documented, already-fixed bug (the `wait_settle(page, 0.3)` re-paying a full
  networkidle wait — see section 8, "Correction (initial version of this change had a bug)").
- The 3-attempt time-slot retry loop with date re-selection (not just re-reading the same DOM) —
  see 9.2's open question above for why this may still not be sufficient.
- Real Chrome / persistent-profile experiment (`BROWSER_CHANNEL`, `BROWSER_USER_DATA_DIR`) to test
  whether a real browser on a real IP gets easier CAPTCHA challenges — explicitly still an
  unconfirmed hypothesis, not a shipped fix, opt-in and off by default on the VM.

If asked to improve this further, the two directions with actual evidence behind them (per 9.2)
are: (a) root-causing why `no_time_slots` is 5/5 so far — possibly by logging/inspecting the raw
`vastuvotukuupaevad` calendar attribute's timing relative to the actual per-slot query, not just
retrying blindly — or (b) reducing the booker's own critical-path time below the ~30s observed
race window, which at a current best case of 36s is not yet consistently achieved even once.

### Deploy status as of this writing (2026-08-02) — read before pushing or pulling anything

- Fix commit `5dc496b` (section 9.1) exists **only** in the local git checkout on the machine
  syncing this folder. It has **not** been pushed to `origin/gcp-booker` on GitHub, and the GCP VM
  has **not** pulled it — confirmed the VM's `run_monitor.sh` executes successfully right now
  (5 clean `time ./run_monitor.sh` runs, no `IndentationError`), meaning the VM's current
  checkout predates the contributor's three broken commits and is unaffected by any of this.
  **Do not `git pull` on the VM before pushing the fix to origin first** — pulling now would pull
  whatever is currently on `origin/gcp-booker`, which (unless already pushed) is still the broken
  state from 9.1.
- Recommendation made to the human at the end of this session: **hold off on pushing/deploying
  this fix to the VM until the follow-up work on the open question in 9.2 is also done** (a
  separate thread/session was planned to investigate booker speed / the `no_time_slots` root
  cause). Reasoning: that follow-up will likely touch the same files (`estonia_d_visa_booker.py`
  at minimum), so doing one combined push + VM pull + `DRY_RUN=1` verification pass once both sets
  of changes exist avoids a two-step deploy and reduces the chance of the VM sitting on a
  half-fixed intermediate state. When it is time to deploy: push to `origin/gcp-booker`, `git
  pull` on the VM, then verify with `DRY_RUN=1 ./run_booker_dryrun.sh` (or equivalent manual env
  vars) before trusting it live in cron — do not skip straight to relying on the next real
  slot-detection event as the first real test of a fix.

## 10. Aug 2026 session #2 (2026-08-02, later the same day): browserless flow proven end to end, session lifetime measured, root cause revised

This section supersedes parts of section 9 — read the corrections in 10.2 before acting on
anything in section 9. Everything below was measured directly against the live site on the VM, not
inferred from reading code. The output of this session is a written implementation spec,
`SPEC - Parked Session Booker Daemon.md`, which is the authoritative design document going
forward; this section records how it was arrived at and what it depends on.

### 10.1 What this session did

Instrumented the existing Playwright booker (`BOOKER_TRACE=1`), captured every HTTP request it
makes, then reimplemented the entire booking flow with plain `requests` and ran it against the
live site — including a real submit and delete. Then measured how long an authenticated session
survives. Three artifacts came out of it: `probe_http_flow.py`, `test_session_longevity.py`, and
the spec.

### 10.2 Corrections to section 9 — do not act on the superseded versions

1. **The 36-100s booker durations in 9.2 are stale.** They predate the event-driven-wait and
   prefetch commits. A traced dry-run on current code measured **23.2s** end to end. The
   improvements in `c7df3e8`/`21a88fa` did work; section 9.2's table describes older code.
2. **"~30 second race window" (section 2.14, repeated in 9.2) is wrong as a general figure.** It
   came from a single observation. `alerts_history.log` shows availability episodes lasting
   **90-110 seconds**, recurring 4-7 minutes apart. The booker was not losing a 30-second race; it
   was losing a ~10-15 second one, repeatedly, inside a much longer window.
3. **9.2's two hypotheses for `no_time_slots` are both resolved.** Hypothesis (a), the booker is
   too slow, is correct. Hypothesis (b), the `vastuvotukuupaevad` list is structurally stale, is
   **not** needed to explain the data and is not supported: F7 below shows the dropdown tracks live
   inventory. The gap between reading the date list and getting an empty slot answer was 2-12
   seconds, which is simply enough time for another party to take it. The human owner independently
   reproduced the same failure by hand in ~10-15 seconds, which is the strongest evidence.
   **An earlier draft of this analysis claimed the two answers were ~1 second apart and therefore
   structurally contradictory. That was an overstatement of the log timestamps and was withdrawn.**
4. **9.3's list of "already optimized, don't re-suggest" is now partly obsolete**, because the
   whole Playwright layer it describes is being replaced. Background CAPTCHA prefetching and the
   `race` mode remain valid ideas; persistent Chromium, event-driven DOM waits and the flatpickr
   retry loop become irrelevant once there is no browser.

### 10.3 Measured facts (F1-F10 in the spec — summarised here so this file stays self-contained)

- **F1.** The entire flow is ordinary Symfony form POSTs to `https://broneering.mfa.ee/en/` with a
  rotating CSRF token. A fully browserless client completed a real booking at 04:48 UTC. No DOM,
  no flatpickr, no Stimulus, no Turbo JS required.
- **F2.** Both reCAPTCHAs are enforced server-side. Posting either gating step with an empty
  `g-recaptcha-response` returns HTTP 200 with page text `"Booking failed"`. Tested explicitly on
  both; there is no bypass.
- **F3.** CapSolver solves measured at 14.6 / 15.8 / 19.8 / 25.0s (mean ~19s). All HTTP work in a
  complete booking totals ~2.5s. **CAPTCHA is now ~90% of the critical path**; everything else is
  rounding error.
- **F4.** The browser is irrelevant to CAPTCHA difficulty. The solver works on its own
  infrastructure from the sitekey and page URL alone; our client never renders the widget. Proven
  by a browserless client booking successfully — the site does not bind the token to our IP.
- **F5. SUPERSEDED — see F5-corrected below.** The original finding was: "a session lasts exactly
  1440 seconds (24 minutes) and activity does not extend it" — established at t+22.0s, first dead
  response at t+1464.8s while polling every 15s throughout, with 1440 matching PHP's default
  `session.gc_maxlifetime`. The death signature is still correct: the response carries a CSRF
  token but `input[name="broneering[kuupaev]"]` is gone (bounced to the start of the form).
- **F5-corrected (2026-08-02, later the same day). A session expires after roughly 95–100
  REQUESTS, not after any fixed duration.** Three runs at three cadences: ~98 requests / 1442s
  polling every 15s; ~95 requests / 138s polling every 2s with date refreshes on top; ~100
  requests / 189–192s polling every 2s, five consecutive deaths in a single run. Durations differ
  by more than 10×; request counts do not. **The 1440s figure was an artifact of that one
  measurement's 15-second polling interval**, and `gc_maxlifetime = 1440` matching it was a
  coincidence that made a wrong conclusion look well-supported. This is the clearest cautionary
  tale in this file: one measurement plus a plausible mechanism is not evidence. Practical
  consequence — polling faster spends the seat's budget faster, so renewal must be driven by
  request count (`SEAT_RENEW_AFTER_REQUESTS`, default 70), with the time threshold kept only as a
  secondary backstop.
- **F6.** Once past the CAPTCHA gate, a date's free-times query needs no CAPTCHA and takes
  195-620ms (median ~490ms over 95 polls). Repeatable indefinitely at zero cost.
- **F7.** The service dropdown reflects **live inventory**, not a schedule. Proven accidentally:
  our test booking consumed one of Abu Dhabi's two slots and D-visa disappeared from the dropdown;
  deleting it brought the service back with both slots.
- **F8.** **You cannot establish a session for a service that is not currently offered.** Both New
  Delhi and Abu Dhabi refused to advance past service selection when D-visa was absent. This is the
  binding constraint on any "keep a session warm" design — a session can only ever be acquired
  opportunistically, after detection.
- **F9.** Availability arrives in bursts: 2026-07-31 episodes at 05:57, 06:04, 06:30, 06:35, 06:40
  UTC, each ~90-110s, separated by 4-7 minutes. 2026-07-30: 08:02, 11:08, 14:06. Because episodes
  cluster more tightly than a session expires, **one session covers several episodes** — this is
  what makes the chosen design viable despite F8. (How many, exactly, depends on the poll
  cadence rather than on elapsed time — see F5-corrected.)
- **F10.** Slot inventory does not churn on its own: Abu Dhabi held a steady 2 slots across 93
  consecutive polls over 24 minutes. The site does not spontaneously release and re-take slots.

### 10.4 Two incidents worth recording

1. **The monitor was down for ~45 minutes and nobody would have known.** `git pull` brought
   the contributor's `lxml`-using monitor onto a VM whose venv never had `lxml` installed (it postdates
   the venv). Every cron tick died with `bs4.exceptions.FeatureNotFound`; `grep -c "MONITOR
   FAILURE" monitor.log` showed 135. The failure-alert cooldown (1.5h) meant a single Telegram
   alert covered the whole outage. Fixed with `venv/bin/pip install lxml`, and hardened
   permanently: both `estonia_visa_monitor.py` and the new scripts now fall back to
   `html.parser` when `lxml` is missing, since `lxml` buys a few milliseconds on a page this size
   and the monitor's entire job is to not be down. **Lesson: never `git pull` onto the VM without
   re-running `pip install -r requirements.txt`, and never let a missing optional dependency be an
   outage.**
2. **A dummy test booking was left live on Abu Dhabi and removed D-visa from its dropdown
   entirely.** `probe_http_flow.py`'s cleanup matched the confirmation page's only `<form>` —
   whose action is empty and whose sole field is the CSRF token — instead of the actual
   `<a href="/en/delete?id=...">Delete</a>` link nested inside it, so it POSTed a no-op. The
   booking survived, consumed one of two slots, and (with the other slot gone) took D-visa off the
   dropdown until deleted by hand. Fixed: links now take priority over forms. **This is also how
   F7 was discovered, so the bug paid for itself — but any future submit-and-delete test must
   verify the delete, not assume it.**

### 10.5 New artifacts

| File | Purpose |
|---|---|
| `probe_http_flow.py` | Browserless replay of the full flow, `PROBE_BOOK=1` to submit+delete. **The proven HTTP recipe — reuse this code, don't rewrite it.** |
| `test_session_longevity.py` | Establishes one session, parks it, polls free-times on a timer. Read-only by construction (contains no `broneeri` key at all). `AUTO_SERVICE=1` falls back to any offered service so it can run outside D-visa windows. |
| `BOOKER_TRACE=1` in `estonia_d_visa_booker.py` | Per-step timing, full request/response capture with POST bodies, flatpickr-month and checkbox-visibility diagnostics. Off by default. |
| `BOOKER_NO_SCREENSHOTS=1` | Dry-runs take ~13 full-page screenshots a live run never pays; this makes dry-run timings representative. |
| `SPEC - Parked Session Booker Daemon.md` | The implementation spec. 42 numbered requirements, cost model, failure modes, test plan, and a required changelog format. |

### 10.6 The decision taken, and the reasoning

Three options were put to the owner: (1) drop the browser only, 25s -> ~20s, no ongoing cost;
(2) a parked-session daemon, 25s -> ~1s, ~$6-15/month; (3) don't optimise, just instrument and
wait for data. **Option 2 was chosen**, explicitly on the grounds that the owner is willing to pay
$20-30/month to secure a single appointment, and that speed is now confirmed (not suspected) to be
the failure mode.

Core idea: pay the ~20s CAPTCHA cost once, early, then sit parked on the calendar page — where
free-times can be checked every 2 seconds for free — and submit the instant a slot appears. F8
means the seat can only be acquired after detection; F9 means one seat then covers several
episodes.

Two owner requirements were made explicit and are encoded as R1-R4 in the spec: **the monitor must
be untouched and must keep alerting even if the daemon fails completely** (so a human can always
book manually), and **every failure must log which step failed**.

### 10.7 Hypotheses retired this session — do not revive

- Checkboxes hidden from the automation (measured `attached: 2, visible: 2`).
- The flatpickr day-click silently failing (`could not find a clickable flatpickr day` = 0 across
  all 5 real runs; the click fired every time).
- Multi-date fallback would have rescued the 5 failures (New Delhi offered exactly one date each
  time). Still worth implementing — the current booker giving up after one date is a real bug —
  but it is not why those five failed.
- Real Chrome / warm browser profile / residential IP getting easier CAPTCHAs (F4). The
  `BROWSER_CHANNEL`, `BROWSER_HEADLESS`, `BROWSER_USER_DATA_DIR` machinery is now known-pointless
  and should not be carried forward.
- Polling keeping a session alive (F5).
- A 24/7 pre-solved CAPTCHA token reserve — still rejected, but now for a sharper reason: the
  chosen design refreshes tokens *only while a seat is live*, which bounds the cost to ~$0.04/hr of
  active holding instead of ~$35/month continuous.

### 10.8 Open question the implementer must resolve first

**Can the available-dates list be refreshed inside a live seat without a new CAPTCHA?** A seat
acquired during episode 1 needs to see a *new* date appearing in episode 2. Unknown whether the
step-5 response carries a fresh `data-vastuvotukuupaevad`, or whether another no-CAPTCHA route
exists.

**Update:** implemented as `resolve_r17_refresh_dates()` in `estonia_booking_http.py` (a step-4
replay), plus a self-healing detector in the daemon that reacquires a fresh seat if the refresh
looks suspect (empty results while a trigger is still fresh — see §11.2 below). It was never
isolated and verified against the live site on its own, but the daemon it's part of went on to
complete a real, successful booking end to end in production (`BOOKING_SUCCESS` in `daemon.log`,
2026-08-05), which required the whole parked-seat-across-episode-gaps flow — including this
refresh — to have worked well enough in practice. Treat it as "working, not formally verified in
isolation" rather than "unresolved."

### 10.9 Deploy status as of the end of this session

- `estonia_visa_monitor.py` is **live and healthy** on the VM after the `lxml` fix, logging
  `NOT available` normally at the 10s cadence.
- Commits `5dc496b` (the contributor's regression fixes from section 9.1) and `63a0ddd` **have now been
  pushed and pulled onto the VM** — section 9's warning about them being unpushed is resolved.
- The Playwright booker remains the live booking path (`ENABLE_BOOKER` set, cron pointed at
  `estonia_visa_monitor_booker_hook.py`). It is unchanged apart from the opt-in tracing. Nothing
  from this session altered live booking behaviour.
- The daemon does not exist yet. Implementation was deliberately handed to a separate, cheaper
  session working from the spec, with a required changelog mapping every requirement ID so the
  result can be reviewed against the spec rather than by reading all the code.

## 11. The daemon's design decisions and requirements, folded in from `SPEC - Parked Session
Booker Daemon.md` (the spec itself has been deleted as part of the v0 public-library cleanup)

The spec's job was to hand a fresh implementation session a complete, self-contained brief. It
numbered every requirement `R1`-`R42` so a required changelog (`IMPLEMENTATION_CHANGELOG.md`)
could map implementation back to spec line-by-line for review. That numbering has now been
scrubbed from the code comments and test names throughout the codebase, because the owner judged
it no longer useful once the implementation is done and working — a comment that says "handles
the edge case where the date list looks stale" doesn't need "(R17)" appended to be understood, and
tying comments to a document that no longer exists is a liability, not a feature. This section
exists so that the reasoning the spec captured isn't lost along with the numbering. The measured
facts it was built on (F1-F10) are already summarised in §10.3 above; what follows is everything
else worth keeping: the design itself, what was required and why, the cost model, the failure
modes it was built to survive, and what was tested.

### 11.1 The core idea and what a real episode looks like under it

Getting past the CAPTCHA gate costs ~20 seconds, but the resulting session ("a **seat**") then
stays alive for a long time and can check real free-times for free (F5-corrected: bounded by
request count, not a clock — see §10.3). So: pay the 20 seconds once, early, then sit parked on
the calendar page and watch. When a slot appears, submit immediately using a pre-solved spare
CAPTCHA token, rather than starting a fresh ~20s solve at the exact moment speed matters most.

A worked example of the timeline this produces, given F9's "episodes cluster 4-7 minutes apart"
and the ~20-minute operating seat lifetime that was chosen (see §11.2):

```
T+0s     Monitor's cron tick detects D-visa. Writes a trigger file.
T+0.5s   Daemon notices the trigger file.
T+0.5s   Daemon starts a CAPTCHA solve AND begins the HTTP flow in parallel.
T+20s    CAPTCHA lands. Daemon completes the flow and is parked on the calendar.
         >>> SEAT ACQUIRED. Also starts keeping a spare CAPTCHA token fresh. <<<
T+20s    Polls free-times every 2s. If a slot is already there, books it now.
...      Episode ends, D-visa disappears. Daemon keeps the seat and keeps polling.
T+300s   Next episode. A slot appears in the poll within 2s.
T+301s   Daemon submits, using the pre-solved spare token.
         >>> BOOKED, ~1 second from slot appearing. <<<
T+1200s  Seat is 20 minutes old. Daemon acquires a replacement seat (1 CAPTCHA)
         while the old one is still alive, then swaps, well ahead of expiry.
```

The constraint that shapes all of this is F8: a seat can only ever be acquired opportunistically,
after D-visa is actually detected — never held continuously in advance. F9 (episodes cluster
tighter than a seat's lifetime) is what makes one seat cover several episodes despite that
constraint.

### 11.2 What was required, and why — by category

These are the decisions the spec locked in before implementation started. They describe intent;
for exact current behaviour, read the code (`estonia_booker_daemon.py`, `estonia_booking_http.py`)
and its comments, which are now self-explanatory without needing this section as a decoder key.

**Non-regression (the highest priority of all of it).** `estonia_visa_monitor.py` must never be
modified, the existing six-line staggered 10s cron must keep working unchanged, and no failure or
hang in the daemon may ever affect the monitor — separate process, separate lock, separate log. If
the daemon is stopped or never started, behaviour must be identical to a system with no daemon at
all. Rationale, stated explicitly by the owner: even if automated booking fails completely, he
must still get the alert and be able to book by hand. Detection is the safety net and must never be
coupled to the booking experiment.

**Trigger mechanism.** The monitor's hook writes a small trigger file on detection (wrapped so any
exception there can never touch the alerting path); the daemon polls for a fresher timestamp every
0.5s. The daemon does not run its own independent site check by default — that would triple
request volume against a government portal that already added a CAPTCHA specifically to deter
this kind of traffic. (`DAEMON_SELF_DETECT` exists as an explicit opt-in, default off.)

**Seat lifecycle.** On trigger, if no seat is live, acquire one (CAPTCHA solve plus the HTTP flow
run concurrently). Operate on a 20-minute working lifetime even though the measured hard ceiling
is longer (§10.3, F5-corrected) — the margin is deliberate: renewing slightly early costs a
fraction of a cent, renewing too late loses the seat mid-episode, and the ceiling was only ever
measured under one specific polling cadence. Renewal must be *overlapping* — acquire and verify a
replacement while the old seat is still alive, then swap atomically, never leaving a gap. Only
renew if D-visa is still actually offered (F8); if not, let the old seat expire and go back to
waiting. Detect seat death *positively* — a response with a CSRF token but no date input field —
rather than inferring it from an empty slot list, because "seat is dead" and "this date genuinely
has no free times right now" must never be confused. Cap concurrency at one live seat plus at most
one in-flight replacement, to bound CAPTCHA spend.

**Slot polling.** Poll free-times on a short fixed interval while a seat is live. Query dates
*sequentially*, never in parallel — the server tracks a "currently selected date" in session
state, and concurrent queries against the same session can corrupt it. An empty result on every
date is the normal, expected case and must never be treated as an error.

**Booking.** Book immediately on the first poll that returns any slot — no extra validation round
trips, because speed is the entire point of this design. If the preferred date yields no slots,
try every other available date before giving up (the old Playwright booker's biggest known gap was
giving up after exactly one date). Booking needs a CAPTCHA token; use the pre-solved reserve, and
if it's empty or stale, solve on demand and log loudly that the fast path was missed. After a
confirmed booking, keep monitoring and keep the daemon running — round 6 (§12.3-12.4 of
`IMPLEMENTATION_CHANGELOG.md`, if still present) later changed the "stop once booked" behaviour
entirely into "keep hunting for anything strictly earlier," at the owner's explicit request; see
that changelog (or its addendum) for the reasoning, since it postdates and supersedes this
original requirement.

**CAPTCHA token reserve.** While a seat is live, keep one freshly-solved spare token on hand,
refreshed well before its usable age expires, and never hand out one older than a hard age cap —
discard and solve fresh instead. Only run the reserve while a seat is live, which is what keeps
the cost bounded (§11.3). Use the primary solver alone for the reserve, not a racing multi-provider
strategy — nothing ever waits on a background reserve solve, so racing would double cost for zero
latency benefit; racing is reserved for the on-demand fallback path, where latency is everything.
Cap total solves per hour as a runaway-spend guard: on breach, stop solving, alert, and keep the
monitor and the existing seat running regardless. Poll the solver's account balance periodically
and alert below a low-balance threshold — a reserve that silently runs dry leaves the system slower
than having no daemon at all, with no visible signal, which is exactly what this alert prevents.

**Logging and alerting.** Every state transition (seat acquired/renewed/swapped/died/expired, poll
result, slot found, booking attempted/succeeded/failed, token refreshed/stale, balance low, rate
limit hit) gets one UTC ISO-8601 log line, with elapsed-time-since-trigger included. Every poll is
also appended to a CSV (timestamp, seat age, date, slot count/times, query latency, seat-alive
flag) — this dataset is what would finally answer, with data instead of guesswork, whether a given
embassy's slots ever really exist. Any booking failure dumps the full response HTML to disk and
references the path in the log, because "it didn't work" with no artifact is what made the early
real failures so expensive to diagnose (see §10.4's incidents and §12.1 of the changelog). Telegram
fires on seat-acquired, booking success/failure (naming which step failed), low balance, rate
limit, and daemon start/clean-shutdown — deliberately never on routine polls, which would be pure
noise at a 2-second cadence.

**Operations.** Ship as a systemd unit, `Restart=always`, running as the same user and with the
same env vars as the monitor. Handle `SIGTERM` cleanly (finish in-flight work, log shutdown, exit
0). A single-instance lock so two daemons can never run concurrently. Every new env var documented
with its default (now in `README.md`, since `BOOKER_README.md` was merged into it).

### 11.3 Cost model

CapSolver reCAPTCHA v2 solves were priced at roughly $0.0008 each. Combined seat-renewal cost
(~1 solve per ~20 minutes of active holding) plus the token-reserve cost (~1 solve per ~75s of
active holding) came to roughly **$0.10/hour of actively holding a seat**. Given F9's ~3-8 episode
clusters/day and up to ~40 minutes of active holding per cluster, that worked out to **roughly
$6-15/month** in practice. The hourly solve-rate cap (§11.2) bounds the absolute worst case — a
seat somehow held permanently — at roughly $35/month. The account balance at spec-writing time was
$7 (~8,750 solves), comfortable for development but the balance alert (§11.2) is what matters once
this runs unattended for real.

### 11.4 Failure modes the daemon was built to survive

| Failure | Required behaviour |
|---|---|
| CAPTCHA solver down or out of credit | Log, alert, keep the seat and keep polling — monitor unaffected |
| Seat dies early (site restart, IP change) | Detect via the positive death signature, log, re-acquire if D-visa is still offered |
| D-visa disappears while parked | Keep the seat, keep polling |
| Site returns 5xx / times out | Exponential backoff, capped, never spin |
| Site markup changes | Any step that can't find its expected element raises with the step name, dumps the HTML, alerts — never fails silently |
| Booking submitted, confirmation unclear | Do **not** mark it booked; alert the owner to check the portal manually (a real 2026-08-03 event submitted a real booking with an ambiguous confirmation check — one date-format marker could never match the site's actual rendering, and there was no negative check either, so "no positive marker" and "the site said 'Booking failed'" looked identical; both were fixed, and the structural `<a href="/en/delete?id=...">` link is now the primary positive signal, checked first, with a hard negative override) |
| Two slots grabbed simultaneously by daemon and owner | Owner-facing risk only; the booked-state flag is what stops all further daemon attempts |
| Daemon crash loop | systemd restart with a backoff; alert if restarts happen too many times in too short a window |

### 11.5 What was tested, and against what

Never against New Delhi (the real target embassy) — always against an embassy with standing
availability (Abu Dhabi, Cairo, Astana), so testing never competes with a real booking attempt.
The spec's test plan covered: the pure preference/fallback logic as unit tests (this is what
`test_daemon_units.py` now covers, expanded well beyond the original plan across several rounds of
fixes — see that file's own module docstring); a live seat acquisition and a multi-minute poll
loop against a real embassy; forcing an early overlapping renewal and a forced seat-death past its
lifetime, to prove both paths behave correctly; a full `DAEMON_DRY_RUN=1` run that stops right
before the final submit; one real submit-plus-delete against a real embassy, with explicit
verification that the delete actually succeeded (see §10.4's second incident — a prior test's
cleanup silently no-opped and left a live dummy booking); killing the daemon mid-poll to confirm
the monitor is unaffected; and pointing the solver credentials at an invalid key to confirm
graceful degradation rather than a crash loop. `applicant_data_dummy.json` (safe, non-personal
values) is what all of the above is meant to run against.

### 11.6 Explicit non-goals (still true, worth keeping in mind before "improving" this)

- Do not modify `estonia_visa_monitor.py`, or its cron schedule/cadence.
- Do not delete the old Playwright booker (`estonia_d_visa_booker.py`) — it stays as an
  intentional fallback, not dead code.
- Do not reintroduce Playwright/browser-profile experiments into the HTTP daemon path — F4 (§10.3)
  already showed the browser has no effect on CAPTCHA difficulty.
- Do not build a continuously-running, 24/7 CAPTCHA token reserve — the chosen design's
  only-while-a-seat-is-live reserve was deliberately chosen over that for cost reasons (§11.3).
- Do not add an LLM anywhere in the runtime path — considered and rejected repeatedly; see §2.

### 11.7 Operational quirks worth remembering (not otherwise recorded in this file)

- Always invoke Python by its absolute venv path (`venv/bin/python3`) in cron/systemd contexts —
  shell activation has been observed to fail intermittently under cron.
- `REQUESTS_CA_BUNDLE` must point at a combined CA bundle file. The site does not send its full TLS
  certificate chain, and `requests` (unlike a browser) does not chase the AIA extension to build
  the rest of the chain itself.
- `run_monitor.sh` holds all real secrets and is intentionally not committed to git — it exists
  only on the VM. (See the main README for how a fresh VM sets this file up.)
- When editing the crontab by hand over SSH, build the file via a heredoc and load it with
  `crontab <file>` — browser-based SSH terminals have been observed to mangle multi-line pastes
  into `nano`.
