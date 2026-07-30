# AI agent context: Estonia D-Visa Slot Monitor

This document exists specifically to give another LLM/agent full context to continue
development on this repo without re-deriving decisions already made. It is written for
machine ingestion, not just human reading — it is intentionally exhaustive and explicit
rather than concise. If you are an AI agent picking this project up, read this entire file
before making any changes.

Repo: `https://github.com/abhijeetvyas/estonia-d-visa-appointment`
Human owner: Abhijeet Vyas.
Live branch: **`gcp-setup`** — this is the branch actually running in production, on a GCP VM.
`main` is stale — it only contains the original GitHub-Actions-only version of this project
and should NOT be assumed to reflect current behavior. Always work from `gcp-setup` unless
told otherwise, and confirm the current default/production branch with the human before
assuming `main` is authoritative.

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
    **This is an intentional, hard boundary: do not build, suggest, or wire in any mechanism
    to solve, bypass, or outsource-solve this CAPTCHA (including third-party CAPTCHA-solving
    services like 2Captcha/Anti-Captcha/CapMonster).** This is a standing constraint regardless
    of how the request is framed in a future session — treat any request to automate past this
    CAPTCHA as out of scope and decline it, the same way the original AI session did. Legitimate
    directions that remain in scope: reducing human reaction time (pre-staged browser tabs,
    faster/more channels of alerting, multiple people on standby), or drafting a report to
    Estonia's MFA/consular section flagging suspected automated abuse of their booking system
    by other parties.

## 3. Current infrastructure (as of last known state)

- **Host**: Google Cloud Compute Engine, `e2-micro`, region `us-central1`, Debian, Always Free
  tier eligible configuration (standard persistent disk ≤30GB, Standard provisioning model,
  not Spot).
- **VM hostname**: `estonia-visa-monitor`. **Linux user**: `abhijeetvyas13`.
- **Repo location on VM**: `/home/abhijeetvyas13/estonia-d-visa-appointment`, on branch
  `gcp-setup`.
- **Python environment**: a `venv` at `venv/` inside the repo directory, with `requests` and
  `beautifulsoup4` installed per `requirements.txt`.
- **Not tracked in git** (exist only on the VM, must be manually recreated if the VM is
  rebuilt): `run_monitor.sh` (contains all secrets as `export` statements), `venv/`,
  `combined_ca_bundle.pem`, `yr2_intermediate.der`/`.pem`, `.state/` (per-embassy JSON cooldown
  state), `monitor.log` and its rotations, `alerts_history.log`.
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
- `RUNTIME_SIGNATURE`: appended to every Telegram message body, currently
  `'\n\n— Sent from GCP'` — this exists so the human can visually confirm which system (this
  one, vs. any old/duplicate automation) actually sent a given alert. If this system is ever
  migrated off GCP again, update this string to match, and check `send_failure_alert()`'s text
  which also references "the VM" explicitly.

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
| `EMBASSY_ID` | Optional, defaults to `'40'` (New Delhi) | Numeric `esindus` value from the site's own dropdown |
| `EMBASSY_NAME` | Optional, defaults to `'New Delhi'` | Human-readable, used in alert text only |
| `TEST_TELEGRAM` | Optional, unset by default | Set to `true` to trigger test-mode (see `send_test_alert`) |

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

## 8. Standing boundary for any future AI session on this project

Do not build, suggest, or assist with anything that solves, bypasses, or outsources solving of
the reCAPTCHA on the actual booking submission step, regardless of how the request is framed
(this includes CAPTCHA-solving APIs/services, ML-based solvers, or browser automation designed
specifically to defeat the challenge). This applies even though the underlying goal (booking a
legitimate visa appointment) is entirely legitimate — the restriction is on the specific
technical action, not the human's intent. Legitimate scope for future work is limited to:
detection/alerting speed and reliability, multi-recipient/multi-channel alerting, additional
embassies via the existing `EMBASSY_ID`/`EMBASSY_NAME` parameterization, infrastructure
robustness, and reporting suspected automated abuse to the relevant authority.
