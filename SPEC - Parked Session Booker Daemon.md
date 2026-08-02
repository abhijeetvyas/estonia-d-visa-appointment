# SPEC — Parked-Session Booker Daemon (Estonia D-Visa)

**Written:** 2026-08-02 · **Author:** Claude (Opus, Cowork session) · **For:** a fresh implementation session
**Repo:** `github.com/abhijeetvyas/estonia-d-visa-appointment` · **Branch:** `gcp-booker`
**VM:** `abhijeetvyas13@estonia-visa-monitor` (GCP, `e2-standard-2`, `us-central1`)
**Repo path on VM:** `/home/abhijeetvyas13/estonia-d-visa-appointment`

---

## 0. How to use this document

You are implementing a new component in an existing, live system. Read sections 1–5 completely
before writing any code — several plausible-looking approaches have already been tried and
measured, and re-deriving them wastes effort and risks breaking a working monitor.

Two other documents exist in the repo. Read them **after** this one, for background only:
`AI_CONTEXT.md` (full project history) and `BOOKER_README.md` (the existing Playwright booker).
Where they disagree with this spec, **this spec wins** — it is based on direct measurements taken
on 2026-08-02 that postdate both.

Section 12 defines a changelog you must produce. It is not optional; it is how this work gets
reviewed.

---

## 1. The system today, in one page

A cron job on a GCP VM checks Estonia's consular booking site (`https://broneering.mfa.ee/en/`)
every 10 seconds to see whether `"Long-stay visa (D-visa) application"` appears in the service
dropdown for the New Delhi embassy (`esindus` value `40`). When it does, it sends Telegram
messages and places a Twilio phone call, then launches a Playwright-driven booker that tries to
complete the booking automatically.

**The monitor works.** It runs in ~1 second per check, sustains a true 10-second cadence with zero
overlap-skips, and has correctly detected every availability window.

**The booker has never succeeded.** Five real trigger events, five `no_time_slots` results. The
booker reached the calendar every time, saw a date listed as available, asked for that date's time
slots, and got an empty list.

**Root cause (established 2026-08-02):** the booker is too slow. It takes ~25 seconds from trigger
to submit, and slots are being taken by someone else within roughly 10–15 seconds. The owner
independently reproduced this by hand — he opened the site within seconds of the alert, clicked
through the form, and by the time he reached the calendar the slot was gone. Roughly 10–15
seconds elapsed.

**This spec's job:** cut the time from "a slot becomes free" to "we have submitted a booking" from
~25 seconds to ~1 second.

---

## 2. Confirmed facts — do not re-investigate these

Everything here was measured directly on 2026-08-02, not inferred. Evidence is cited so you can
re-verify if something looks wrong, but do not spend effort re-discovering it.

### F1. The entire booking flow is plain HTTP form POSTs. No browser is required.

A full browserless booking was completed successfully at 04:48 UTC using `requests` only —
including the final submit. Playwright, flatpickr, Stimulus and Turbo JavaScript are all
unnecessary. *Evidence: `probe_http_flow.py` run, log at `/tmp/probeC.log`.*

### F2. Both reCAPTCHAs are enforced server-side.

Submitting either the service-selection step or the final booking with an empty
`g-recaptcha-response` returns HTTP 200 with the page text `"Booking failed"`. There is no way
around them. *Evidence: probe steps 3 and 6, both tested with an empty token first.*

### F3. A CAPTCHA solve takes 14–25 seconds and is ~90% of the critical path.

Four measured CapSolver solves: 14.6s, 15.8s, 19.8s, 25.0s. Mean ≈ 19s. By contrast, **all** the
HTTP work in a complete booking totals ~2.5 seconds.

### F4. The browser has no effect on CAPTCHA difficulty.

CapSolver solves the challenge on its own infrastructure, given only the sitekey and page URL, and
returns a token string. Our client never renders or interacts with the widget. A fully browserless
client booked successfully, proving the site does not bind the token to our IP.

**Consequence:** the `BROWSER_CHANNEL`, `BROWSER_HEADLESS` and `BROWSER_USER_DATA_DIR`
experiments in `estonia_d_visa_booker.py` are testing a hypothesis that is now disproven. Do not
carry them into the new component.

### F5. A session lasts exactly 1440 seconds (24 minutes), and activity does not extend it.

Measured precisely: session established at t+22.0s, first dead response at t+1464.8s — a lifetime
of 1442.8s. The test polled every 15 seconds throughout, so this is an **absolute** expiry, not an
idle timeout. 1440s is PHP's default `session.gc_maxlifetime`.

Death signature: the response still contains a CSRF token, but the date field
(`input[name="broneering[kuupaev]"]`) is gone — you have been bounced to the start of the form.
*Evidence: `test_session_longevity.py`, log at `/tmp/session_test.log`, CSV in `session_tests/`.*

### F6. Once past the CAPTCHA gate, querying free times is free and fast.

A date-selection POST needs no CAPTCHA and took 195–620ms (median ~490ms) across 95 polls. This
can be repeated indefinitely for the life of the session at zero monetary cost.

### F7. The service dropdown reflects live inventory.

Proven accidentally: our test booking consumed one of Abu Dhabi's two slots and D-visa vanished
from the dropdown; deleting the booking brought it straight back with both slots. So "D-visa is in
the dropdown" means "at least one slot exists right now."

### F8. You cannot establish a session for a service that is not currently offered.

Both New Delhi (04:47) and Abu Dhabi (05:32) refused to proceed past service selection when D-visa
was absent. **This is the single most important constraint on the design** — see §4.

### F9. Availability arrives in bursts, not continuously.

From `alerts_history.log`, 2026-07-31: episodes at 05:57, 06:04, 06:30, 06:35, 06:40 UTC, each
lasting ~90–110 seconds, separated by 4–7 minutes (one 25-minute gap). Earlier, 2026-07-30: 08:02,
11:08, 14:06.

**Critical implication:** a 24-minute session established during one episode is still alive for
the next several. That is what makes this design work.

### F10. Slot inventory does not churn on its own.

Abu Dhabi held a steady 2 slots across 93 consecutive polls over 24 minutes, with no
appear/disappear behaviour. The site does not spontaneously release and re-take slots; when they
move, someone moved them.

---

## 3. Dead hypotheses — do not re-explore

| Hypothesis | Status | Why |
|---|---|---|
| Time-slot checkboxes are hidden from the automation | **Dead** | Measured `attached: 2, visible: 2` |
| The flatpickr date-click silently fails | **Dead** | `could not find a clickable flatpickr day` count = 0 across all 5 real runs |
| Multi-date fallback would have saved the 5 failures | **Dead** | New Delhi offered exactly one date each time |
| CAPTCHA can be skipped | **Dead** | F2 |
| A real Chrome / warm browser profile gets easier CAPTCHAs | **Dead** | F4 |
| The site blocks plain HTTP clients | **Dead** | The monitor has done exactly this every 10s for weeks |
| Tightening the cron interval below 10s helps | **Dead** | Detection was never the bottleneck; see `AI_CONTEXT.md` §9.2 |
| Polling keeps a session alive | **Dead** | F5 |
| A continuously-refreshed CAPTCHA token reserve, running 24/7 | **Rejected on cost** | ~$35/mo for tokens that mostly go stale unused. The design below refreshes tokens *only while a seat is live*, which is bounded and cheap |

---

## 4. The design

### 4.1 The core idea

Getting past the CAPTCHA gate takes ~20 seconds, but the resulting session then lives for 24
minutes and can check real free-times for free. So: **pay the 20 seconds once, early, then sit on
the calendar page and watch.** When a slot appears, submit immediately.

We call a live, authenticated, parked session a **seat**.

### 4.2 The constraint that shapes everything (F8)

You cannot acquire a seat unless D-visa is currently in the dropdown. So a seat cannot be held
continuously — only opportunistically, starting from the moment the monitor first detects an
availability window.

This is workable because of F9 + F5: episodes cluster 4–7 minutes apart, and a seat is good for
20 minutes (measured ceiling 24; we operate on 20 — see R10). One seat acquired at the start of a cluster covers every subsequent episode in that
cluster at ~1 second latency.

### 4.3 Timeline of a real episode under this design

```
T+0s     Monitor's 10s cron tick detects D-visa. Writes a trigger file.
T+0.5s   Daemon notices the trigger file.
T+0.5s   Daemon starts a CAPTCHA solve AND begins the HTTP flow in parallel.
T+20s    CAPTCHA lands. Daemon completes the flow and is parked on the calendar.
         >>> SEAT ACQUIRED. Also starts keeping a spare CAPTCHA token fresh. <<<
T+20s    Polls free-times every 2s. If a slot is already there, books it now.
...      Episode ends, D-visa disappears. Daemon keeps the seat and keeps polling.
T+300s   Next episode. A slot appears in the poll within 2s.
T+301s   Daemon submits, using the pre-solved spare token.
         >>> BOOKED, ~1 second from slot appearing. <<<
T+1200s  Seat is 20 minutes old (our working limit). Daemon acquires a replacement
         seat (1 CAPTCHA) while the old one is still alive, then swaps. Hard
         expiry is not until 1440s, so the swap has 4 minutes of margin.
```

### 4.4 Component layout

| File | Status | Role |
|---|---|---|
| `estonia_visa_monitor.py` | **DO NOT MODIFY** | The 10s cron monitor. Independent safety net. |
| `estonia_visa_monitor_booker_hook.py` | Modify minimally | Add: write a trigger file on detection. |
| `estonia_booker_daemon.py` | **NEW** | The daemon. Everything in this spec. |
| `estonia_booking_http.py` | **NEW** | Reusable HTTP flow module (§5). |
| `estonia_d_visa_booker.py` | Leave in place | The old Playwright booker. Kept as fallback; disabled via env. |
| `probe_http_flow.py`, `test_session_longevity.py` | Reference | Working, tested code for the HTTP flow. **Read these first — they are the proven recipe.** |

---

## 5. The exact HTTP recipe

Every request goes to `https://broneering.mfa.ee/en/`. The CSRF token
(`broneering[_token]`) **rotates on every response** and must be re-extracted each time.
Use one `requests.Session` per seat so cookies persist.

Working, tested implementations of every step below already exist in `probe_http_flow.py`.
**Reuse that code rather than rewriting from the tables here.**

### Headers

```python
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

turbo_headers = {
    "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://broneering.mfa.ee/en/",
    "Origin": "https://broneering.mfa.ee",
}
```

For the `edasi` and `broneeri` steps, override `Accept` to `text/html, application/xhtml+xml`.

### Step 0 — GET landing page
`GET /en/` → extract `input[name="broneering[_token]"]`.

### Step 1 — select embassy → returns service dropdown
```
broneering[esindus]=40
broneering[isikuteArv]=1
g-recaptcha-response=
broneering[__dynamic_error]=
broneering[_token]=<token>
turbo=
```
Success: response contains `select[name="broneering[toiming]"]`.
Check whether any option's value is `pikaajaline_viisa`. **If absent, stop — a seat is impossible (F8).**

### Step 2 — select service (Turbo refresh)
Same as step 1 plus `broneering[toiming]=pikaajaline_viisa`. Still `turbo=`, still no CAPTCHA.

### Step 3 — CAPTCHA GATE → applicant form
```
broneering[esindus]=40
broneering[toiming]=pikaajaline_viisa
broneering[isikuteArv]=1
g-recaptcha-response=<SOLVED TOKEN>
broneering[__dynamic_error]=
broneering[_token]=<token>
edasi=
```
Returns 302 → follow (requests does this automatically).
Success: response contains `input[name="broneering[broneeringuIsikud][0][eesnimi]"]`.
Failure signature: page text contains `"Booking failed"`.

### Step 4 — applicant details → calendar
```
broneering[broneeringuIsikud][0][eesnimi]=<first_name>
broneering[broneeringuIsikud][0][perenimi]=<surname>
broneering[broneeringuIsikud][0][synniaeg]=<DD.MM.YYYY>
broneering[broneeringuIsikud][0][telefon]=<+91...>
broneering[broneeringuIsikud][0][epost]=<email>
broneering[broneeringuIsikud][0][viisataotluseNumber]=<number>
broneering[_token]=<token>
edasi=
```
Success: response contains an element with `data-controller="broneering"`.
Read available dates from its `data-vastuvotukuupaevad` attribute — a JSON array of `YYYY-MM-DD`.

> **Note:** BeautifulSoup lowercases attribute names. `data-etteBron` is only reachable as
> `data-ettebron`.

**A seat is now established.** Steps 0–4 take ~2s of HTTP plus the CAPTCHA wait.

### Step 5 — query free times for one date (NO CAPTCHA, repeatable)
```
broneering[kuupaev]=<DD.MM.YYYY>
broneering[markused]=
g-recaptcha-response=
broneering[__dynamic_error]=
broneering[_token]=<token>
turbo=
```
Parse `input[name="broneering[vastuvotud][]"]`. Each checkbox's `value` is an internal slot ID
(e.g. `2249077`) — **not** the time. The displayed `HH:MM` is in `label[for="<checkbox id>"]`.
Pair them via the `for`/`id` relationship, never by DOM proximity.

### Step 6 — submit the booking
```
broneering[kuupaev]=<DD.MM.YYYY>
broneering[vastuvotud][]=<slot id>
broneering[markused]=<specifying_information>
g-recaptcha-response=<SOLVED TOKEN>
broneering[__dynamic_error]=
broneering[_token]=<token>
broneeri=
```
Success markers in page text: `"Booked time"`, `"Add new booking"`, or the date in `DD.MM.YYYY`
form alongside `"Delete"`.

### Step 7 — delete (test cleanup only)
The confirmation page contains `<a href="/en/delete?id=...">Delete</a>`. That link is **inside**
the page's only `<form>`, whose action is empty — matching the form instead of the link posts a
no-op and leaves a live booking. Follow the link.

---

## 6. Requirements

Each requirement has an ID. The changelog in §12 must reference these IDs.

### 6.1 Non-regression — highest priority

- **R1.** `estonia_visa_monitor.py` must not be modified in any way.
- **R2.** The existing cron schedule (six staggered lines, 10s cadence) must keep working
  unchanged. Telegram alerts and Twilio calls must continue to fire on detection regardless of
  daemon state.
- **R3.** No failure, crash, hang, or resource exhaustion in the daemon may affect the monitor.
  Separate process, separate lock, separate log.
- **R4.** If the daemon is stopped or was never started, system behaviour must be identical to
  today.

> Rationale: the owner's explicit requirement is that even if automated booking fails entirely, he
> still gets the alert and can book by hand. Detection is the safety net and must never be
> coupled to the experiment.

### 6.2 Trigger mechanism

- **R5.** Modify `estonia_visa_monitor_booker_hook.py` so that on detection it writes
  `.state/trigger_<EMBASSY_ID>.json` containing `{"ts": <unix time>, "services": [...]}`. Write
  atomically (temp file + `os.replace`).
- **R6.** This write must be wrapped so that any exception is caught and logged without affecting
  the alerting path.
- **R7.** The daemon polls that file every 0.5s and treats a timestamp newer than the last one it
  saw as a fresh trigger.
- **R8.** Do **not** have the daemon run its own independent site check by default. That would
  triple request volume against a government portal that has already added a CAPTCHA. Provide
  `DAEMON_SELF_DETECT=1` as an opt-in, default off.

### 6.3 Seat lifecycle

- **R9.** On trigger, if no live seat exists, acquire one: start the CAPTCHA solve and steps 0–2
  concurrently, then steps 3–4.
- **R10.** **Operate on a 20-minute seat.** 1440s (24 min) is the measured hard ceiling (F5), but
  every design decision, log message and calculation should treat a seat as good for **20 minutes**
  and renew on that schedule. The 4-minute gap is deliberate margin: the 1440s figure comes from a
  single measurement on one embassy, and being wrong in the direction of "renewed slightly too
  early" costs $0.0008, while being wrong the other way means losing the seat mid-episode.
  `SEAT_TTL_SECONDS` = 1440 (the ceiling, for death-detection sanity checks),
  `SEAT_RENEW_AFTER_SECONDS` = 1200 (the operating number), both env-overridable.
- **R11.** Renewal must be **overlapping**: acquire a new seat while the old one is still alive,
  verify it reaches the calendar, then atomically swap. Never leave a gap.
- **R12.** Only renew if D-visa is still available (F8). If it is not, log clearly, let the old
  seat expire, and return to waiting for a trigger.
- **R13.** Detect seat death positively: a response containing a CSRF token but **no**
  `input[name="broneering[kuupaev]"]` means the seat is dead (F5). Do not infer death from an
  empty slot list — distinguishing "seat dead" from "date genuinely has no free times" is
  essential.
- **R14.** Hold at most one live seat plus at most one in-flight replacement. Cap concurrency to
  avoid runaway CAPTCHA spend.

### 6.4 Slot polling

- **R15.** While a seat is live, poll free-times every `SLOT_POLL_INTERVAL_SECONDS` (default 2).
- **R16.** Query dates **sequentially**, not in parallel. The server tracks a "currently selected
  date" in session state and concurrent queries may corrupt it. At ~0.5s per date and typically
  1–3 dates, sequential is fast enough.
- **R17.** **Open question you must resolve and document:** can the available-dates list be
  refreshed within a live seat without a new CAPTCHA? Check whether the step-5 response carries a
  fresh `data-vastuvotukuupaevad`. If not, find another no-CAPTCHA route (e.g. re-POSTing step 4,
  or a plain `GET /en/` within the session) and document what works. This matters because a seat
  acquired during episode 1 must be able to see a *new* date that appears in episode 2. Log the
  finding explicitly.
- **R18.** If polling returns an empty list for every date, that is normal — keep polling. Do not
  treat it as an error.

### 6.5 Booking

- **R19.** The moment a poll returns ≥1 slot on any date, book immediately. Speed here is the
  entire point — no extra validation round trips.
- **R20.** Date preference: latest available date that is ≥3 days from today; fall back to the
  earliest available. (Unchanged from current behaviour — see `choose_date()`.)
- **R21.** Time preference: earliest slot between 12:00 and 14:00; fall back to the earliest slot
  overall. (Unchanged — see `choose_time()`.)
- **R22.** **If the preferred date yields no slots, try every other available date before giving
  up.** The current booker picks one date and returns `no_time_slots` without trying the rest.
- **R23.** Booking requires a CAPTCHA token (F2). Use the reserve (§6.6). If the reserve is empty
  or stale, solve on demand, log loudly that this happened, and proceed.
- **R24.** After a confirmed booking: set the persistent `booked` flag in
  `.state/booker_state_<EMBASSY_ID>.json`, send the success Telegram, and stop all further booking
  attempts. Keep monitoring and keep the daemon running.
- **R25.** Never book twice. Check the `booked` flag before every submit, not just at startup.
- **R26.** `DAEMON_DRY_RUN=1` must run everything including seat acquisition and polling, but stop
  immediately before the step-6 submit, log exactly what it would have booked, and Telegram it.

### 6.6 CAPTCHA token reserve

- **R27.** While a seat is live, keep one freshly-solved token available. Refresh every
  `TOKEN_REFRESH_SECONDS` (default 75; tokens are valid ~120s).
- **R28.** Only run the reserve while a seat is live. This bounds cost — see §7.
- **R29.** Track token age. Never use a token older than `TOKEN_MAX_AGE_SECONDS` (default 100);
  discard and solve fresh instead.
- **R30.** Use CapSolver alone for the reserve. Do **not** use `CAPTCHA_PROVIDER=race` here.
  Racing exists to cut latency variance, and nothing ever waits on a background reserve — racing
  would double cost for zero benefit. Keep `race` for the on-demand fallback path (R23), where
  latency is everything.
- **R31.** Cap solves at `MAX_SOLVES_PER_HOUR` (default 60) as a runaway guard. On breach: stop
  solving, send a Telegram alert, keep the monitor and existing seat running.
- **R32.** Poll CapSolver's balance every 15 minutes (`POST https://api.capsolver.com/getBalance`
  with `{"clientKey": ...}`). Telegram an alert below `CAPSOLVER_LOW_BALANCE_USD` (default 2.00).
  A reserve that silently runs dry leaves the system slower than it is today with no visible
  signal — this alert is what prevents that.

### 6.7 Logging and alerting

- **R33.** All daemon output to `daemon.log`, one UTC ISO-8601 timestamped line per event, using
  the existing `log_line()` style. Add a logrotate config matching the monitor's.
- **R34.** Log every state transition explicitly: `TRIGGER_RECEIVED`, `SEAT_ACQUIRING`,
  `SEAT_ACQUIRED`, `SEAT_RENEWING`, `SEAT_SWAPPED`, `SEAT_DIED`, `SEAT_EXPIRED`, `POLL_RESULT`,
  `SLOT_FOUND`, `BOOKING_ATTEMPT`, `BOOKING_SUCCESS`, `BOOKING_FAILED`, `TOKEN_REFRESHED`,
  `TOKEN_STALE`, `BALANCE_LOW`, `RATE_LIMIT_HIT`. Include elapsed-since-trigger on each.
- **R35.** Append every poll to `daemon_polls.csv`: `utc, seat_age_s, date, slot_count,
  slot_times, query_ms, seat_alive`. This is the dataset that will finally show whether New Delhi
  slots ever actually exist.
- **R36.** On any booking failure, dump the full response HTML to `daemon_dumps/` and reference
  the path in the log. A failure that only says "it didn't work" is what has made the last five
  failures so expensive to diagnose.
- **R37.** Telegram on: seat acquired (with dates seen), booking success, booking failure
  **including which step failed**, balance low, rate limit hit, daemon start and clean shutdown.
  Do **not** Telegram routine polls.
- **R38.** Reuse the existing `RUNTIME_LABEL` signature convention on all messages.

### 6.8 Operations

- **R39.** Ship a systemd unit `estonia-booker-daemon.service`, `Restart=always`, `RestartSec=10`,
  running as `abhijeetvyas13`, with the same env vars as `run_monitor.sh`.
- **R40.** Handle `SIGTERM` cleanly: finish any in-flight booking, log shutdown, exit 0.
- **R41.** A single-instance lock so two daemons cannot run at once.
- **R42.** Document every new env var in `BOOKER_README.md`, with defaults.

### 6.9 Configuration summary

| Variable | Default | Purpose |
|---|---|---|
| `EMBASSY_ID` / `EMBASSY_NAME` | `40` / `New Delhi` | Target embassy |
| `SEAT_TTL_SECONDS` | `1440` | Measured session lifetime |
| `SEAT_RENEW_AFTER_SECONDS` | `1200` | Start overlapping renewal |
| `SLOT_POLL_INTERVAL_SECONDS` | `2` | Free-times poll cadence |
| `TOKEN_REFRESH_SECONDS` | `75` | Reserve refresh interval |
| `TOKEN_MAX_AGE_SECONDS` | `100` | Discard tokens older than this |
| `MAX_SOLVES_PER_HOUR` | `60` | Runaway spend guard |
| `CAPSOLVER_LOW_BALANCE_USD` | `2.00` | Balance alert threshold |
| `DAEMON_DRY_RUN` | unset | Everything except final submit |
| `DAEMON_SELF_DETECT` | unset | Opt-in independent detection |
| `CAPSOLVER_API_KEY` | — | Required |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Required |
| `APPLICANT_DATA_REAL` / `APPLICANT_DATA_DUMMY` | — | Applicant data paths |
| `RUNTIME_LABEL` | `GCP` | Identifies the sending machine |

---

## 7. Cost model

CapSolver reCAPTCHA v2 ≈ **$0.0008 per solve** (verify against the live dashboard rate).

| Activity | Rate | Monthly |
|---|---|---|
| Seat renewals while an episode cluster is active | 1 per 20 min | ~$0.06/hr of active holding |
| Token reserve while a seat is live | 1 per 75s | ~$0.04/hr of active holding |
| **Combined, while actively holding a seat** | | **~$0.10 per hour** |

From F9, roughly 3–8 episode clusters per day. Each cluster holds a seat for up to ~40 minutes
(one acquisition plus one renewal). That is ~2–5 hours of active holding per day, or
**roughly $6–15/month**.

The `MAX_SOLVES_PER_HOUR` cap (R31) bounds the worst case at 60 × $0.0008 × 24 × 30 ≈ $35/month
even if something goes wrong and a seat is held permanently.

Current balance is **$7**, which is ~8,750 solves — comfortable for testing, but R32's balance
alert matters once this runs unattended.

---

## 8. Failure modes to handle explicitly

| Failure | Required behaviour |
|---|---|
| CapSolver down or out of credit | Log, Telegram, keep the seat and keep polling. Monitor unaffected. |
| Seat dies early (site restart, IP change) | Detect via R13, log, attempt re-acquisition if D-visa is still offered. |
| D-visa disappears while parked | Keep the seat, keep polling. **Log whether polling still works** — currently unknown. |
| Site returns 5xx / times out | Exponential backoff, cap at 30s, never spin. |
| Site markup changes | Any step failing to find its expected element must raise with the step name, dump the HTML, and Telegram. Never fail silently. |
| Booking submitted, confirmation unclear | Do **not** set the `booked` flag. Telegram a ⚠️ telling the owner to check the portal manually. |
| Two slots grabbed simultaneously by daemon and owner | Owner-facing risk only; ensure the `booked` flag stops all further attempts. |
| Daemon crash loop | systemd `RestartSec=10`; log crash count; if >5 restarts in 10 min, Telegram. |

---

## 9. Test plan

**Never test against New Delhi (`EMBASSY_ID=40`).** Use Abu Dhabi (`244`), Cairo (`45`) or Astana
(`49`), which have standing availability.

- **T1.** Unit: `choose_date()` / `choose_time()` preference rules, including the R22 fallback
  across multiple dates.
- **T2.** Seat acquisition against Abu Dhabi. Assert calendar reached, dates parsed, ~20s total.
- **T3.** Poll loop for 5 minutes. Assert every poll succeeds, CSV is written, no CAPTCHA spent
  after acquisition.
- **T4.** Overlapping renewal: set `SEAT_RENEW_AFTER_SECONDS=60` and confirm a new seat is
  acquired and swapped with no gap in polling.
- **T5.** Seat-death detection: set `SEAT_RENEW_AFTER_SECONDS=99999`, run past 1440s, confirm
  `SEAT_DIED` fires with the R13 signature and not before.
- **T6.** `DAEMON_DRY_RUN=1` end to end. Confirm it stops before submit and reports the intended
  date/time.
- **T7.** One real submit + delete against Abu Dhabi, mirroring `probe_http_flow.py`'s
  `PROBE_BOOK=1`. **Verify the deletion succeeded** — a previous run left a live dummy booking that
  removed D-visa from Abu Dhabi's dropdown entirely until it was cleaned up.
- **T8.** Kill the daemon mid-poll; confirm the cron monitor is unaffected and still logging.
- **T9.** Set `CAPSOLVER_API_KEY` to an invalid value; confirm graceful degradation and a Telegram
  alert rather than a crash loop.

Dummy applicant data (`applicant_data_dummy.json`) is safe to use for all of the above.

---

## 10. Explicit non-goals

- Do not modify `estonia_visa_monitor.py`.
- Do not change the cron schedule or its cadence.
- Do not delete `estonia_d_visa_booker.py` — it stays as a fallback.
- Do not reintroduce Playwright, browser-profile or real-Chrome experiments (F4).
- Do not build a 24/7 CAPTCHA reserve (§3).
- Do not add an LLM anywhere in the runtime path. This has been considered and rejected
  repeatedly; see `AI_CONTEXT.md` §2.

---

## 11. Suggested implementation order

1. `estonia_booking_http.py` — extract the proven flow from `probe_http_flow.py` into reusable
   functions (`establish_seat`, `poll_slots`, `submit_booking`, `delete_booking`). Test with T1–T2.
2. Seat lifecycle: acquisition, TTL tracking, death detection, overlapping renewal. T3–T5.
3. Resolve R17 (can the date list refresh within a seat?) and document the answer.
4. Trigger file: hook change (R5–R6) plus daemon watcher (R7).
5. Token reserve, rate limit, balance alerts (R27–R32).
6. Booking path including multi-date fallback (R19–R26). T6–T7.
7. Logging, CSV, Telegram, dumps (R33–R38).
8. systemd unit, docs, resilience tests (R39–R42, T8–T9).

Stop and flag if step 3 reveals that the date list **cannot** be refreshed within a seat — that
would materially change the design and is worth a decision before continuing.

---

## 12. Required output: the changelog

Create `IMPLEMENTATION_CHANGELOG.md` in the repo root. It will be reviewed against this spec, so
completeness matters more than brevity. Use exactly this structure.

```markdown
# Implementation Changelog — Parked-Session Booker Daemon
Implemented by: <model/session>   Date: <date>   Branch: <branch>   Commits: <hashes>

## 1. Requirement status
| ID | Requirement (short) | Status | Where | Notes |
|----|--------------------|--------|-------|-------|
| R1 | Don't modify monitor | Done / Partial / Skipped / Blocked | file:line | ... |
(every ID R1–R42, none omitted)

## 2. Files
| File | New/Modified | Purpose | Lines |

## 3. Deviations from spec
For each: what the spec said, what was done instead, and the evidence that
prompted the change. If the spec was wrong, say so plainly and show why.

## 4. Open question R17 — date-list refresh within a seat
State the finding, the evidence, and the resulting design decision.

## 5. Test results
| ID | Command run | Result | Evidence |
Include real output, not just "passed".

## 6. Unresolved / not implemented
Anything left undone and why.

## 7. Reviewer notes
What you are least confident about, and what should be checked most carefully.
```

**Rules for the changelog:**

- Every requirement ID R1–R42 must appear. Do not silently drop any.
- If something was not implemented, mark it `Skipped` with a reason. Do not mark it `Done`.
- If a spec requirement turned out to be wrong or impossible, say so in §3 with evidence — that is
  useful, not a failure.
- Test results must include real command output. "Tested and works" is not acceptable.
- Confirm explicitly in §1 that `estonia_visa_monitor.py` is byte-identical to before
  (`git diff --stat estonia_visa_monitor.py` should show nothing).

---

## 13. Context you may need

**Existing helpers worth reusing:** `send_telegram()`, `log_line()`, `load_state()` /
`save_state()`, `is_already_booked()` / `mark_booked()`, `choose_date()`, `choose_time()`,
`_solve_recaptcha_capsolver()` — all in `estonia_d_visa_booker.py`.

**Known environment quirks:**

- Always invoke Python by absolute venv path (`venv/bin/python3`). Shell activation has been
  observed to fail intermittently under cron.
- `REQUESTS_CA_BUNDLE` must point at `combined_ca_bundle.pem`. The site does not send its full TLS
  chain and `requests` does not chase AIA. Browsers hide this; Python does not.
- `lxml` may not be installed. Both new scripts should fall back to `html.parser` rather than
  crashing — this took the monitor down for ~45 minutes on 2026-08-02.
- `run_monitor.sh` holds all secrets and is not in git. It exists only on the VM.
- When editing crontab, build the file via heredoc and `crontab <file>`. Browser-based SSH
  terminals mangle multi-line pastes into `nano`.
```
