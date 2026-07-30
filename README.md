# Estonia D-Visa Slot Monitor

Checks Estonia's consular booking site (`broneering.mfa.ee`) on a tight schedule and alerts
you the moment the "Long-stay visa (D-visa) application" service becomes bookable for a given
embassy. No LLM/agent runs at check time — this is a plain, deterministic script, so the cost
per check is effectively zero regardless of how often it runs.

Alerts go out over two channels: Telegram (every single check where a slot is found, uncapped,
to multiple recipients) and an actual phone call via Twilio (throttled, since a ringing phone
survives silent mode/Do Not Disturb in a way a push notification doesn't).

**Looking for the automated booker add-on** (Playwright-driven auto-booking once a slot is
detected, CAPTCHA solving, date/time selection, etc.)? That's a separate layer on top of this
monitor — see `BOOKER_README.md`.

---

## Architecture, and why each piece was chosen

**Why not an agent (e.g. Gumloop) running this on a schedule?**
The task itself never requires judgment at runtime — fetch a page, look for one specific
string, alert if found. An agent re-reasoning about that every single scheduled run burns
tokens for no benefit, since the logic doesn't change between runs. The right place for an
LLM here is at build time (writing/debugging the script), not at execution time.

**Why not rely on GitHub Actions' built-in `schedule` trigger?**
It's kept in this repo (`.github/workflows/estonia-visa-monitor.yml`) as a fallback/reference,
but it is not the primary way this runs. GitHub's own cron scheduler is explicitly "best
effort," and for anything more frequent than about an hour on a low-traffic personal repo, it
can drift badly — schedules that should fire every 15 minutes were observed firing as
infrequently as every 1–1.5 hours. That's not acceptable for a monitor whose whole value is
speed.

**Why a self-hosted VM instead of any cloud scheduler (GitHub Actions, cron-job.org, etc.)?**
Two separate problems pushed this direction:
1. Scheduling reliability — a VM's own OS-level `cron` fires exactly on time, with no
   dependency on a third party's shared scheduler queue.
2. Network-layer issues — requests from shared/rotating cloud IP ranges (like GitHub Actions
   runners) intermittently failed with connection timeouts and SSL errors against this
   specific site. Running from a single, dedicated VM removes that variable.

**Why Google Cloud's `e2-micro` over Oracle Cloud's Always Free tier?**
Oracle's free tier is more generous on paper (more CPU/RAM), but it explicitly reclaims
"idle" Always Free instances — defined as 95th-percentile CPU utilization under 20% over a
7-day period. A cron job that wakes up for a couple of seconds every 30 seconds and is
otherwise idle the rest of the time is exactly the profile Oracle's policy targets for
reclamation. GCP's `e2-micro` Always Free tier has no equivalent idle-reclamation policy,
making it more predictable for a workload that's supposed to sit idle almost all the time.

**Why does the script use `requests`/`BeautifulSoup` instead of a real browser (Playwright etc.)?**
The site doesn't currently require JavaScript execution or solve a CAPTCHA at this specific
step, so a lightweight HTTP client is sufficient and far cheaper to run. If the site adds a
CAPTCHA/JS challenge later, this approach would need to move to a headless browser instead.

**Why Telegram *and* a phone call, instead of just one channel?**
Telegram is free and instant but easy to miss if a slot opens and closes within minutes. A
phone call is much harder to miss, and — critically — travels over the cellular voice network
rather than the internet, so it can still reach you if WiFi/data happens to be off. Telegram
stays uncapped as the "log of record"; the phone call is throttled since three unanswered
calls in a row means calling a fourth time immediately isn't help, it's noise.

---

## One-time setup

### 1. Create the GCP VM

- console.cloud.google.com → Compute Engine → VM instances → Create Instance.
- **Region**: one of the Always Free eligible regions — `us-west1`, `us-central1`, or
  `us-east1`.
- **Machine type**: `e2-micro` (this specific shape is the free-tier-eligible one).
- **Boot disk**: any recent Debian/Ubuntu, **Standard persistent disk** (not "Balanced" —
  that's billed), 30GB or less.
- **Provisioning model**: Standard (not Spot — Spot instances aren't Always Free eligible).
- Leave inbound firewall rules off — this script only makes outbound requests, no inbound
  access is needed at all.
- The GCP console's "Monthly estimate" panel will show a non-zero cost during setup — this is
  expected and not a mistake. It shows list price before the Always Free credit is applied;
  the credit is applied later, on your actual invoice.

### 2. SSH in and install dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/abhijeetvyas/estonia-d-visa-appointment.git
cd estonia-d-visa-appointment
git checkout gcp-setup   # this branch has the GCP-specific fixes; main is the original GitHub-Actions-only version
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If cloning a private repo fails with `Password authentication is not supported`, you need a
GitHub Personal Access Token (fine-grained, scoped to this repo, with **Contents: Read-only**
permission) to use as the password when prompted.

### 3. Fix the SSL certificate chain issue

This site's server doesn't send its full TLS certificate chain — browsers silently patch this
via automatic intermediate-certificate fetching, but Python's `requests` library doesn't. Fetch
the missing intermediate once and bundle it in:

```bash
curl -s http://yr2.i.lencr.org/ -o yr2_intermediate.der
openssl x509 -inform DER -in yr2_intermediate.der -out yr2_intermediate.pem
cat "$(venv/bin/python3 -c 'import certifi; print(certifi.where())')" yr2_intermediate.pem > combined_ca_bundle.pem
```

(If this ever breaks again months from now, it's likely because Let's Encrypt rotated their
intermediate — re-run `openssl s_client -connect broneering.mfa.ee:443 -servername broneering.mfa.ee -showcerts`
to find the current "CA Issuers" URL and repeat the steps above.)

### 4. Set up Telegram

- Message [@BotFather](https://t.me/BotFather), run `/newbot`, save the token it gives you.
- Have every intended recipient message the bot at least once (Telegram blocks bots from
  messaging users who haven't initiated contact first).
- Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find each recipient's
  `chat.id`.

### 5. Set up Twilio (for the phone call escalation)

- Buy a Twilio phone number with Voice capability (Console → Phone Numbers).
- If on a trial account, verify every destination number under Verified Caller IDs (trial
  accounts can only call verified numbers).
- Console → Develop → TwiML Bins → Create new bin, with content:
  ```xml
  <Response>
    <Say loop="3">Estonia visa slot is now available. Check Telegram and book immediately.</Say>
  </Response>
  ```
- Copy the bin's Request URL — this is `TWILIO_TWIML_URL` below.

### 6. Create the wrapper script with all secrets

```bash
cat > run_monitor.sh << 'EOF'
#!/bin/bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="chat_id_1,chat_id_2"
export REQUESTS_CA_BUNDLE="/home/YOUR_USERNAME/estonia-d-visa-appointment/combined_ca_bundle.pem"
export TWILIO_ACCOUNT_SID="your_account_sid"
export TWILIO_AUTH_TOKEN="your_auth_token"
export TWILIO_FROM_NUMBER="+1XXXXXXXXXX"
export TWILIO_TO_NUMBERS="+91XXXXXXXXXX,+91YYYYYYYYYY"
export TWILIO_TWIML_URL="https://handler.twilio.com/twiml/EHxxxxxxxxxxxxx"
cd /home/YOUR_USERNAME/estonia-d-visa-appointment
/home/YOUR_USERNAME/estonia-d-visa-appointment/venv/bin/python3 estonia_visa_monitor.py >> monitor.log 2>&1
EOF
chmod +x run_monitor.sh
```

Calling the venv's `python3` by its full path directly (rather than `source venv/bin/activate`
first) is deliberate — under cron, `source`-based activation was found to intermittently not
take effect, causing `ModuleNotFoundError` on some runs.

### 7. Set up cron for 10-second checks

Cron's minimum native granularity is one minute, so six offset lines are used — each firing
once a minute but staggered by 0/10/20/30/40/50 seconds via `sleep` — giving an effective
10-second cadence. All six share the same lock file so overlapping runs skip cleanly instead
of stacking up (expect a higher `SKIPPED` count than at looser intervals, since there's less
slack before the next tick).

```bash
cat > /tmp/current_cron << 'EOF'
* * * * * /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh || echo "$(date -Iseconds) SKIPPED: previous run still in progress" >> /home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log
* * * * * sleep 10; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh || echo "$(date -Iseconds) SKIPPED: previous run still in progress" >> /home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log
* * * * * sleep 20; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh || echo "$(date -Iseconds) SKIPPED: previous run still in progress" >> /home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log
* * * * * sleep 30; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh || echo "$(date -Iseconds) SKIPPED: previous run still in progress" >> /home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log
* * * * * sleep 40; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh || echo "$(date -Iseconds) SKIPPED: previous run still in progress" >> /home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log
* * * * * sleep 50; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh || echo "$(date -Iseconds) SKIPPED: previous run still in progress" >> /home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log
EOF
crontab /tmp/current_cron
crontab -l   # verify it took
```

### 8. Set up log rotation

```bash
sudo tee /etc/logrotate.d/estonia-visa-monitor > /dev/null << 'EOF'
/home/YOUR_USERNAME/estonia-d-visa-appointment/monitor.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 644 YOUR_USERNAME YOUR_USERNAME
    su YOUR_USERNAME YOUR_USERNAME
}
EOF
sudo logrotate -f /etc/logrotate.d/estonia-visa-monitor   # test it rotates cleanly
```

---

## Command reference

Run all of these from `~/estonia-d-visa-appointment` on the VM.

### Running / testing

```bash
./run_monitor.sh                       # one real check against the live site
TEST_TELEGRAM=true ./run_monitor.sh    # sends a test Telegram message + test call to all recipients,
                                        # bypassing the call cooldown, without touching the real site
```

### Viewing logs

```bash
tail -f monitor.log                    # watch live
tail -20 monitor.log                   # last 20 lines
cat alerts_history.log                 # permanent record of every real AVAILABLE/FAILURE event —
                                        # never manually clear this file
ls -la monitor.log*                    # see what's been rotated (monitor.log.1, monitor.log.2.gz, ...)
zcat monitor.log.2.gz                  # view a compressed rotated file
```

### Combined report across all logs (current + rotated + compressed)

```bash
ALL_LOGS=$( { cat monitor.log monitor.log.1 2>/dev/null; zcat monitor.log.*.gz 2>/dev/null; } )
echo "Total runs: $(echo "$ALL_LOGS" | grep -cE "NOT available|AVAILABLE:|MONITOR FAILURE")"
echo "Available: $(echo "$ALL_LOGS" | grep -c 'AVAILABLE:')"
echo "Not available: $(echo "$ALL_LOGS" | grep -c 'NOT available')"
echo "Failures: $(echo "$ALL_LOGS" | grep -c 'MONITOR FAILURE')"
echo "Skipped (overlap): $(echo "$ALL_LOGS" | grep -c 'SKIPPED')"
```

### Verifying cron is actually firing (independent of what the script logs)

```bash
grep -c CRON /var/log/syslog           # total times cron has invoked anything, ever (per this log file)
grep CRON /var/log/syslog | tail -20   # most recent invocations
```

### Cron / crontab management

```bash
crontab -l                             # view current schedule
```
To edit, always build the file via heredoc rather than pasting into the interactive `crontab -e`
editor — browser-based SSH terminals frequently mangle multi-line pastes into `nano`:
```bash
cat > /tmp/current_cron << 'EOF'
<paste desired crontab lines here>
EOF
crontab /tmp/current_cron
```

### Logrotate

```bash
sudo logrotate -d /etc/logrotate.d/estonia-visa-monitor   # dry run
sudo logrotate -f /etc/logrotate.d/estonia-visa-monitor   # force a rotation now
```

### Editing secrets/config

```bash
nano run_monitor.sh                    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TWILIO_* vars
```

### Pulling code updates

```bash
git status                              # confirm you're on gcp-setup and see any local diffs
git pull                                # fetch latest from the tracked branch
```

### Fixing a stale git lock (`index.lock` / `HEAD.lock`)

If `git commit`/`git push`/etc. fail with something like:
```
fatal: Unable to create '.../.git/index.lock': File exists.
Another git process seems to be running in this repository...
```
this is a leftover lock file from a previous git process that didn't clean up after itself
(not an actual concurrent process still running - if you're not mid-`git commit` in another
terminal/editor right now, it's safe to remove). Fix:
```bash
rm -f .git/index.lock    # or .git/HEAD.lock, whichever the error names
git status               # confirm it's back to normal before retrying
```
Then retry the original `git add`/`commit`/`push` command.

---

## Alerting behavior summary

| Event | Telegram | Phone call |
|---|---|---|
| Slot found | Every check, uncapped, all recipients | Up to 3 calls, 5 min apart, then 6h cooldown before a fresh cycle |
| Not found | Nothing (silent) | Nothing |
| Script failure | One alert, then 1.5h cooldown before repeating | Never — failures are frequent/expected during outages and shouldn't ring anyone |
| Test run (`TEST_TELEGRAM=true`) | Always sent | Always placed, ignores cooldown |

---

## Known limitations

- If the site adds a CAPTCHA or JavaScript challenge to this step, this `requests`-based
  approach would stop working and need to move to a headless browser (e.g. Playwright).
- The CSRF token is fetched fresh every single run — this can't be cached, since Estonia's
  booking form requires a valid session token per submission.
- `run_monitor.sh` and the CA bundle/intermediate cert files are intentionally not tracked in
  git (they contain secrets or are generated locally) — if this VM is ever rebuilt, steps 3 and
  6 above need to be redone by hand.
- To reuse this for a different embassy, set `EMBASSY_ID` and `EMBASSY_NAME` as additional
  environment variables in `run_monitor.sh` rather than forking the repo — the script is
  already parameterized for this.
