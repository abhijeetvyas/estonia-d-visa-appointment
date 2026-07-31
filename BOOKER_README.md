# Estonia D-Visa Booker (gcp-booker branch)

This branch adds automatic booking on top of the existing high-frequency monitor.
The original `estonia_visa_monitor.py` is **untouched**.

## Files added

| File | Purpose |
|------|---------|
| `estonia_d_visa_booker.py` | Playwright booker (Stage 2 + Stage 3) |
| `estonia_visa_monitor_booker_hook.py` | Thin wrapper that runs the original monitor and, when a slot is found, launches the booker |
| `applicant_data_dummy.json` | Dummy applicant data (safe to keep in repo for dry-runs) |
| `applicant_data_real.json.example` | Template – copy to `applicant_data_real.json` **on the VM only** |
| `BOOKER_README.md` | This file |
| `persistent_chromium.py` | Optional: keeps one headless Chromium warm via CDP, cuts booking latency (see below) |
| `requirements.txt` | Now also lists `playwright` |

## Three-stage behaviour

1. **Detection** (original monitor, every ~10 s)  
   D-visa option appears → Telegram + throttled phone call (unchanged).

2. **List slots** (booker)  
   After first reCAPTCHA + applicant form → calendar reached → Telegram listing all available dates.  
   Always runs when the booker is triggered (even if already booked).

3. **Book** (booker, only if not already booked)  
   Chooses date/time according to rules → second reCAPTCHA → clicks “Book the appointment” → Telegram success → sets permanent `booked` flag.

### Date / time preference
- **Date**: latest available date that is ≥ 3 days from today. Fallback = earliest available.
- **Time**: earliest slot in 12:00–14:00. Fallback = earliest available slot overall.

### When `booked` flag is set
- Stage 1 still fires.
- Stage 2 still runs and sends the available-slots list.
- Stage 3 is permanently skipped.

### Concurrency
- File lock (`/tmp/estonia_d_visa_booker_<EMBASSY_ID>.lock`) + 90 s cooldown after every booker run.
- If a booker is already running, a Telegram “skipped – already in progress” message is sent.
- Monitor itself continues at 10 s; only the heavy Playwright process is gated.

## VM setup (one-time)

```bash
cd ~/estonia-d-visa-appointment
git fetch origin
git checkout gcp-booker          # or create the branch – see bottom of this file

# install Playwright + browser
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # or: playwright install --with-deps chromium

# real applicant data (NEVER commit this file)
cp applicant_data_real.json.example applicant_data_real.json
nano applicant_data_real.json    # fill in your real details
chmod 600 applicant_data_real.json
```

### New environment variables (add to `run_monitor.sh`)

```bash
export ENABLE_BOOKER=1
export CAPSOLVER_API_KEY="your_capsolver_key"
export APPLICANT_DATA_REAL="/home/abhijeetvyas13/estonia-d-visa-appointment/applicant_data_real.json"
export APPLICANT_DATA_DUMMY="/home/abhijeetvyas13/estonia-d-visa-appointment/applicant_data_dummy.json"
# optional overrides
# export BOOKER_COOLDOWN_SECONDS=90
# export DRY_RUN=1               # for testing – preview only, stops before final submit
# export DRY_RUN_TEST_SUBMIT=1   # with DRY_RUN=1: also submits + deletes a real test booking
# export FORCE_LIST_ONLY=1       # force Stage-2 only
```

**CAPTCHA provider**: defaults to CapSolver (faster observed solve times). Set
`CAPTCHA_PROVIDER=2captcha` plus `TWOCAPTCHA_API_KEY` to fall back to 2captcha
instead — both implementations are kept in the script, selectable via this
one env var (default is `capsolver`, so no need to set it explicitly unless
switching):

```bash
# default — no CAPTCHA_PROVIDER line needed:
export CAPSOLVER_API_KEY="your_capsolver_key"

# to use 2captcha instead:
export CAPTCHA_PROVIDER=2captcha
export TWOCAPTCHA_API_KEY="your_2captcha_key"

# to race both simultaneously and take whichever solves first:
export CAPTCHA_PROVIDER=race
export CAPSOLVER_API_KEY="your_capsolver_key"
export TWOCAPTCHA_API_KEY="your_2captcha_key"
```

**`CAPTCHA_PROVIDER=race`**: fires both CapSolver and 2captcha concurrently for the same
challenge and keeps whichever result arrives first - reduces the odds of a slow solve on one
provider blocking a step, since solve times vary run to run (observed 12-40s+ on CapSolver
alone). Real cost trade-off, not a free win: both are pay-per-successful-solve services, and
since neither has a "cancel this task" API, the losing solve still runs to completion in the
background and still gets billed on the vendor's side - realistically this means paying both
providers on nearly every real attempt, not just the one whose token you end up using. Only
sensible because this runs at real trigger time (rare slot-detection events), not continuously -
the extra cost is a fraction of a cent per real attempt, not an ongoing drain. Requires both
`CAPSOLVER_API_KEY` and `TWOCAPTCHA_API_KEY` set; if only one key is present the "race" still
works, it just trivially finishes via whichever one has a valid key.

**Both CAPTCHAs are pre-solved in the background from the very start of a run**, not just when
each step is reached - neither depends on any page state, only the (assumed-stable) sitekey and
page URL, both known constants before the browser even opens. The second CAPTCHA in particular
historically isn't needed until 50-90s into the run, by which point its background solve is
usually already done, cutting its wait out of the critical path entirely. This is safe by
construction: if the page's real sitekey ever doesn't match what was prefetched, it just falls
back to solving fresh exactly like before - correctness never depends on the prefetch working,
only speed does. Look for `Using prefetched reCAPTCHA token (solved in background)` in the logs
to confirm it's actually kicking in.

### Event-driven waits instead of flat sleeps between form steps

Several steps between page transitions (embassy submit → service dropdown, applicant form
submit → calendar page, etc.) previously used a flat `wait_settle()` sleep (2.5-3s each,
regardless of how long the page actually took) as a safety margin. These now wait directly for
the next step's actual element to appear (`page.wait_for_selector(..., timeout=8000)`) and
proceed the instant it's there - typically far faster than the old fixed buffer. If the element
doesn't show up within that window, it falls back to the original flat-sleep behavior exactly as
before, so this can only be faster, never worse or less reliable. Look for `didn't appear within
8s - falling back to full settle wait` in `booker.log` to see if a given run ever needed the
fallback (if you see this regularly, the 8s window may need lengthening for that step). The same
idea applies to the fixed 2s pre-second-CAPTCHA sleep, now a 2s-ceiling wait for the reCAPTCHA
widget to exist rather than an unconditional 2s pause.

**Correction (initial version of this change had a bug):** the success path originally called
`wait_settle(page, 0.3)` after `wait_for_selector` succeeded, intending a short trailing pause.
`wait_settle()` unconditionally does a full `networkidle` wait (up to 15s) internally before its
own sleep argument even applies - so this was silently re-paying the exact cost the optimization
was meant to remove, on every single run, regardless of the number passed in. Confirmed directly
in a dry-run log: a 16s gap between the captcha1-injected and applicant-page screenshots with no
fallback-timeout line logged, meaning `wait_for_selector` succeeded quickly but `wait_settle()`
then spent up to 15s waiting for network idle anyway - this site's Turbo/websocket traffic rarely
goes fully idle, so that wait routinely ran to its full timeout. Fixed: the success path now uses
a plain `time.sleep(0.3)` instead, since `wait_for_selector` having already succeeded is
confirmation enough that the target element is there.

### Alert when a date is available but no time slot could be secured

Previously, if `no_time_slots` occurred (a date showed as available but every retry came back
with zero bookable checkboxes - see below), this only showed up in `booker.log`, with no
Telegram alert. Now a dedicated ❌ Telegram message is sent in this case, since the Stage-2
"available dates" message doesn't communicate that the actual booking attempt subsequently
failed - you'd otherwise have no way to know from Telegram alone that a real opportunity was
seen and lost.

### Time-slot retry on a date that comes up empty

After selecting a date, the booker waits for that date's time-slot checkboxes to render before
reading them. This can legitimately come back empty even though the calendar listed the date as
available - the calendar's "available dates" list can be a step ahead of the actual per-slot
inventory (another applicant takes the last slot, or a temporary hold expires and frees one back
up, in the seconds between the two checks). The booker now retries up to 3 times: each retry
re-selects the date (re-triggering a fresh server-side query, not just re-reading the same DOM)
and waits again before giving up and reporting `no_time_slots`. Each attempt waits up to 5s for
the checkboxes to render (observed real renders take 1-4s, so this is margin, not a fixed
sleep - `wait_for_selector` returns the instant the element appears rather than blocking for the
full timeout), with a 3s pause between attempts. Look for `Time slots for <date> (attempt N/3):
[...]` in `booker.log` to see how many attempts a given run actually took.

### Point cron at the hook (instead of the original monitor)

Edit `run_monitor.sh` so the final Python line becomes:

```bash
/home/abhijeetvyas13/estonia-d-visa-appointment/venv/bin/python3 \
  estonia_visa_monitor_booker_hook.py >> monitor.log 2>&1
```

(The original `estonia_visa_monitor.py` remains available if you ever want to disable the booker by simply unsetting `ENABLE_BOOKER`.)

### Optional: keep Chromium warm to cut booking latency

Cold-launching Chromium from scratch (`p.chromium.launch()`) was observed
taking 10-30s+ on this VM, and considerably longer under CPU-credit
throttling from back-to-back runs (shared-core `e2` family instances all use
a baseline+burst credit model). That cost lands exactly at the moment speed
matters most - right after a slot is detected. `persistent_chromium.py`
keeps one headless Chromium instance running permanently with a local CDP
port open; the booker attaches to it via Playwright's `connect_over_cdp()`
(a ~1-2s handshake) instead of paying the full cold-start cost. It falls
back to a fresh launch automatically if the persistent instance isn't
reachable, so setting this up is optional - the booker works either way.

Set up as a systemd service so it survives reboots and auto-restarts if
Chromium crashes:

```bash
sudo tee /etc/systemd/system/estonia-chromium.service > /dev/null << 'EOF'
[Unit]
Description=Persistent headless Chromium for Estonia visa booker
After=network.target

[Service]
Type=simple
User=abhijeetvyas13
WorkingDirectory=/home/abhijeetvyas13/estonia-d-visa-appointment
ExecStart=/home/abhijeetvyas13/estonia-d-visa-appointment/venv/bin/python3 persistent_chromium.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now estonia-chromium
sudo systemctl status estonia-chromium   # confirm it's running
```

No env var changes needed in `run_monitor.sh` — the booker checks
`http://127.0.0.1:9222` by default. Override with `PERSISTENT_CHROMIUM_CDP_URL`
if you ever change the port (also update `PERSISTENT_CHROMIUM_CDP_PORT` for
`persistent_chromium.py` itself to match).

To confirm it's actually being used, look for this line in a run's log:
`Attached to persistent Chromium at http://127.0.0.1:9222` — if you instead
see `Persistent Chromium not reachable ... launching a fresh instance`, the
service isn't up (check `sudo systemctl status estonia-chromium` and
`sudo journalctl -u estonia-chromium -n 50`).

### VM sizing: the booker is much heavier than the plain monitor

The base monitor (`estonia_visa_monitor.py`) is a lightweight `requests` call and runs fine
forever on the free `e2-micro` tier. The booker launches a real headless Chromium and solves
two reCAPTCHAs per run - a meaningfully heavier workload. In practice, running several booker
dry-runs back-to-back on `e2-micro` showed clear CPU-credit exhaustion: `e2-micro`/`e2-small`/
`e2-medium` are all **shared-core** machine types (baseline + burst credit model), and sustained
Playwright load burns through the burst credit, causing later runs in a burst to get visibly
slower across every step (not just CAPTCHA-dependent ones) - confirmed directly against the
GCP Monitoring CPU Utilization graph, which showed clear spikes to 200% (both vCPUs pegged)
during test runs.

If you plan to actually rely on the booker (not just occasional dry-run testing), consider
upgrading to a **non-shared-core** machine type - this removes the credit/throttling ceiling
entirely:

```bash
# VM must be stopped first (Console → Compute Engine → your instance → Stop)
gcloud compute instances set-machine-type estonia-visa-monitor \
  --zone=us-central1-<your-zone> \
  --machine-type=e2-standard-2
# then Start again
```

This takes the VM out of Always Free tier eligibility (that's `e2-micro`-specific), so it starts
incurring real billing - check current pricing before committing. Everything else (disk, venv,
cron, systemd services, state files) survives untouched; only the machine type changes.

If `e2-standard-2` hits a `ZONE_RESOURCE_POOL_EXHAUSTED` / capacity stockout error in your zone
(a transient GCP-side issue, not a config problem), try again after a few minutes, or fall back
to another non-shared-core type in the same zone (no migration needed, just another Edit
attempt) - roughly in order of cost: `e2-standard-2` → `n2d-standard-2` → `n1-standard-2` →
`n2-standard-2` → `t2d-standard-2` → `e2-standard-4`. All are 2 vCPU / ~8GB (except
`e2-standard-4`, which is double that), all non-shared-core, all interchangeable for this
purpose - whichever one actually has capacity wins.

**If staying on a shared-core type** (`e2-micro` etc.), it's worth adding a small swap file as a
cheap safety net - `e2-micro` ships with zero swap configured, so a memory spike (e.g. the
persistent Chromium service permanently reserving ~250-300MB) can trigger the OOM killer
outright instead of degrading gracefully:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Real Memory/Disk monitoring (Ops Agent)

The default GCP Monitoring dashboard only shows hypervisor-level metrics (CPU, network, disk
throughput) out of the box - Memory Utilization and Disk Space Utilization panels show "No data
available" until the **Ops Agent** is installed:

```bash
curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
sudo bash add-google-cloud-ops-agent-repo.sh --also-install
```

If, after installing, `sudo systemctl status google-cloud-ops-agent` shows `[API Check] Result:
FAIL, Error code: MonApiPermissionErr` / `LogApiPermissionErr` in its logs, the VM's service
account lacks permission to write to the Monitoring/Logging APIs - this is an instance-level
**access scope** issue, not a package issue. Fix (VM must be stopped): Edit → **Identity and API
access** → **Access scopes** → change to **"Allow full access to all Cloud APIs"** → Save →
Start, then `sudo systemctl restart google-cloud-ops-agent`.

### Running the booker locally instead of the VM

The booker script also runs fine on a personal laptop, not just the VM - useful for faster
iteration while developing/debugging, or for testing whether a real browser on a real
residential IP gets easier CAPTCHA challenges than a headless instance from a GCP datacenter IP
(a real hypothesis, unconfirmed - reCAPTCHA risk-scores on signals like IP reputation and
browser fingerprint, and a fresh headless Chromium on a datacenter IP is about as high-risk a
profile as it gets).

```bash
cd estonia-d-visa-appointment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install chrome     # registers the real Chrome channel with Playwright
```

`applicant_data_dummy.json` is already in the repo, so `DRY_RUN=1` works immediately - no need
to create `applicant_data_real.json` just to test locally. The SSL-certificate-chain workaround
(`combined_ca_bundle.pem`, needed for the plain monitor's `requests` calls) is **not** needed
here - the booker drives an actual browser via Playwright, and browsers already handle that
certificate chain correctly on their own.

Three extra env vars control the real-Chrome experiment, all off by default so VM/live behavior
is completely unaffected:

```bash
export BROWSER_CHANNEL=chrome                       # use real installed Chrome instead of bundled Chromium
export BROWSER_HEADLESS=0                            # run headed (visible window) instead of headless
export BROWSER_USER_DATA_DIR=~/estonia-booker-profile # persist cookies/history across runs
```

Leave all unset to get the original headless-Chromium, fresh-profile-every-time behavior. Note
`BROWSER_CHANNEL`/`BROWSER_HEADLESS` only apply to the fresh-launch fallback path - they have no
effect if a persistent Chromium (`persistent_chromium.py`) is reachable, since that path always
attaches over CDP regardless. `BROWSER_USER_DATA_DIR`, if set, **skips the persistent-Chromium
path entirely** and takes priority - it's a different kind of "persistent" (a warm profile for
fingerprint/history, not a warm process for launch speed) and the two aren't meant to combine.

**Why `BROWSER_USER_DATA_DIR` matters for the experiment:** without it, every run starts from a
completely empty, cookie-less, history-less profile - even with real Chrome, that's still a
fresh/anonymous-looking session every time. A real browser's trust signal comes partly from
session *age*, not just which binary is running. Pointing this at a real directory on disk lets
cookies/local storage/history actually accumulate across runs.

**Where to point it - a safety trade-off, not just a preference:**
- **Recommended: a new, dedicated profile** (e.g. `~/estonia-booker-profile`) used only for this.
  It starts empty on first run, but you can "warm it up" by opening that same profile in real
  Chrome yourself between automated runs (`open -a "Google Chrome" --args --user-data-dir=~/estonia-booker-profile`
  on macOS) and browsing normally for a bit - logging into a Google account, visiting a few
  everyday sites - so it accumulates some organic history rather than staying a blank slate.
- **Not recommended, at least not for a first test: your actual daily Chrome profile.** This
  would give the most authentic signal (years of real history), but you can't run your normal
  Chrome and this automation against the same profile directory at the same time (profile-lock
  conflicts), and anything that goes wrong happens under your real identity/logged-in sessions,
  not a disposable one. Start with a dedicated profile; only consider this later if the dedicated
  one demonstrably isn't enough.

Given both changes here are unconfirmed hypotheses, not established fixes, the honest way to use
this is comparatively: run a few dry-runs with a fresh profile, then a few with a warmed-up
`BROWSER_USER_DATA_DIR` profile, and see whether CAPTCHA difficulty/frequency actually differs.

**Important:** running locally is a testing/iteration tool, not a replacement for the VM. The
entire reason this project runs on a dedicated always-on VM is to catch a slot without a human
or personal machine needing to be present - a local run only works while your laptop is on,
awake, and the script is actively running.

### Two dry-run modes: preview-only, or full submit + delete

`DRY_RUN=1` alone keeps its original behavior unchanged - it stops one screen short of the final
"Book the appointment" click, same as always. That preview never exercises the actual submit
step, which is the one part of the flow most important to trust.

Set `DRY_RUN_TEST_SUBMIT=1` **in addition to** `DRY_RUN=1` to instead run the fuller check: it
clicks the real submit button (using `applicant_data_dummy.json`), confirms success the same way
a live booking does, then immediately deletes the test booking via the site's own "Delete" (or
"Cancel") action on the confirmation page, and sends a Telegram message reporting whether both
the booking and the cleanup delete succeeded. It never calls `mark_booked()` (the persistent
"already booked" state), so this has no effect on live-mode behavior either way.

Both modes are available side by side - pick whichever fits what you're testing:

```bash
# Preview only (original behavior) - stops before the real submit click:
export DRY_RUN=1

# Full test - actually submits and then deletes the test booking:
export DRY_RUN=1
export DRY_RUN_TEST_SUBMIT=1
```

**Be aware this is a real write to Estonia's live booking system**, not a simulation - it creates
an actual appointment record under dummy applicant data (`John Doe`, a fake email, a made-up visa
application number) for the few seconds between submit and delete. It's deleted immediately after,
but running this repeatedly against the production site is worth being deliberate about, not
something to loop casually.

If the delete step ever fails (e.g. the site's delete/cancel button text doesn't match what the
code guesses - `Delete` or `Cancel` - or a second "are you sure?" confirmation step uses different
wording), the booker logs `DRY_RUN: no Delete/Cancel button found...` or `DRY_RUN delete step
failed: ...`, sends a ⚠️ Telegram alert instead of a ✅, and takes a `14_after_delete` screenshot
either way so the actual page markup can be inspected and the selector corrected. **Check the
portal manually if you ever see the ⚠️ message** - a stray test booking may be sitting there under
dummy data until removed.

### Dry-run test on the VM

```bash
cd ~/estonia-d-visa-appointment
source venv/bin/activate
export DRY_RUN=1
export EMBASSY_ID=244          # Abu Dhabi has slots for testing
export EMBASSY_NAME="Abu Dhabi"
export CAPSOLVER_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python3 estonia_d_visa_booker.py
```

Screenshots land in `booker_screenshots/`. No real booking is performed with this preview-only
invocation (add `export DRY_RUN_TEST_SUBMIT=1` above to also exercise the real submit+delete flow
described above).

### Clearing the booked flag

```bash
rm -f .state/booker_state_40.json
# or edit the file and set "booked": false
```

## Creating / pushing the `gcp-booker` branch (from your laptop or the VM)

```bash
# on a machine that has the repo
git checkout gcp-setup
git pull

git checkout -b gcp-booker

# copy the new files into the repo root
# (or on the VM just place them there)

git add estonia_d_visa_booker.py \
        estonia_visa_monitor_booker_hook.py \
        applicant_data_dummy.json \
        applicant_data_real.json.example \
        requirements.txt \
        BOOKER_README.md

git status
git commit -m "Add Playwright D-visa booker with 3-stage flow, slot preference, and concurrency lock"

git push -u origin gcp-booker
```

Then on the GCP VM:

```bash
cd ~/estonia-d-visa-appointment
git fetch origin
git checkout gcp-booker
# continue with the setup steps above
```

The original `gcp-setup` branch and all previous monitor behaviour stay completely intact.
