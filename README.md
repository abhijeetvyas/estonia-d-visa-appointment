# Estonia D-Visa Slot Monitor & Booker

Estonia's visa appointment site rarely has open slots, and when one does open, it's usually
gone within a minute or two. This project watches the site around the clock and, the moment a
slot opens, either alerts you immediately or — if you turn on the optional add-on — books it
for you automatically, faster than a human could click through the form.

It runs on a small cloud server (not your own laptop), so it keeps working even when your
computer is off.

## The two pieces

- **The Monitor** — checks the booking site every 10 seconds. The instant the visa service
  becomes available, it sends you a Telegram message and calls your phone (a call is much
  harder to miss than a notification). This is the base setup and is free to run.
- **The Booker** (optional) — once a slot is detected, this tries to complete the booking
  for you automatically, in about 1 second. It needs a small amount of setup (your real
  applicant details, and a paid CAPTCHA-solving service, a few cents per attempt) because the
  actual booking step requires solving a "I'm not a robot" challenge.

You can run just the Monitor, or the Monitor plus the Booker. Either way, the Monitor never
stops alerting you, even if the Booker isn't set up or crashes — so you can always fall back
to booking it yourself by hand.

**Want the full story of how and why this was built this way** — including approaches that
were tried and abandoned along the way? See `Article - Building a Zero-Agent Visa Monitor with
AI v2.md`. For a deeper technical/engineering log, see `AI_CONTEXT.md`.

---

## What you'll need

- A Google Cloud account (there's a permanently free tier that comfortably runs the Monitor;
  the Booker is heavier and works best on a small paid tier, roughly $10-25/month)
- A Telegram account (for alerts — free)
- A Twilio account (optional, for phone-call alerts — a few cents per call)
- Only if you want the Booker: a CapSolver account (for solving the CAPTCHA automatically,
  roughly $0.001 per solve) and your real visa application details

---

## Setup

### 1. Create the server

- Go to console.cloud.google.com → Compute Engine → VM instances → Create Instance.
- **Region**: `us-west1`, `us-central1`, or `us-east1` (these are eligible for the free tier).
- **Machine type**: `e2-micro` if you only want the Monitor (free). If you also want the
  Booker, use `e2-standard-2` instead — it's heavier and this incurs real (small) billing;
  see "Turning on the Booker" below for how to resize this later if you start on the free tier.
- **Boot disk**: any recent Debian/Ubuntu, standard persistent disk, 30GB or less.
- **Provisioning model**: Standard (not Spot).
- You can leave inbound network access off — this only makes outbound requests.

### 2. Install the monitor

SSH into the new server (the GCP console has a "SSH" button right next to your VM), then:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/abhijeetvyas/estonia-d-visa-appointment.git
cd estonia-d-visa-appointment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. A one-time site quirk to work around

The visa site's server has a small TLS configuration issue that trips up Python (but not
regular web browsers). Run this once to fix it:

```bash
curl -s http://yr2.i.lencr.org/ -o yr2_intermediate.der
openssl x509 -inform DER -in yr2_intermediate.der -out yr2_intermediate.pem
cat "$(venv/bin/python3 -c 'import certifi; print(certifi.where())')" yr2_intermediate.pem > combined_ca_bundle.pem
```

(If this ever stops working months from now, it's likely because the site's certificate
provider rotated something — re-run
`openssl s_client -connect broneering.mfa.ee:443 -servername broneering.mfa.ee -showcerts`
to find the current URL and repeat the steps above.)

### 4. Set up Telegram alerts

- Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and save the
  token it gives you.
- Message your new bot once (anything), then visit
  `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find your `chat.id`.

### 5. Set up phone-call alerts (optional but recommended)

- Buy a Twilio phone number with Voice capability.
- If your Twilio account is on a trial plan, verify the number(s) you want it to call under
  "Verified Caller IDs" (trial accounts can only call verified numbers).
- Create a "TwiML Bin" in the Twilio console with this content:
  ```xml
  <Response>
    <Say loop="3">Estonia visa slot is now available. Check Telegram and book immediately.</Say>
  </Response>
  ```
- Copy that bin's URL — you'll need it below.

### 6. Save your secrets

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

Replace `YOUR_USERNAME` and the placeholder values with your own. Leave the Twilio lines out
entirely if you're skipping phone-call alerts.

### 7. Turn the monitor on

This checks the site every 10 seconds. (A single scheduled task can only fire once a minute at
minimum, so six are used, staggered 10 seconds apart, to add up to a 10-second cadence.)

```bash
cat > /tmp/current_cron << 'EOF'
* * * * * /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh
* * * * * sleep 10; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh
* * * * * sleep 20; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh
* * * * * sleep 30; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh
* * * * * sleep 40; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh
* * * * * sleep 50; /usr/bin/flock -n /tmp/estonia_visa_monitor.lock /home/YOUR_USERNAME/estonia-d-visa-appointment/run_monitor.sh
EOF
crontab /tmp/current_cron
crontab -l   # confirm it took
```

Test it worked with `TEST_TELEGRAM=true ./run_monitor.sh` — you should get a Telegram message
(and a test call, if you set up Twilio) within a few seconds.

### 8. Set up log rotation

So the log file doesn't grow forever:

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
```

**You're done with the base setup.** From here you'll get an alert the moment a slot opens.
Continue below only if you also want it booked automatically.

---

## Turning on the Booker (optional)

The Booker uses a paid CAPTCHA-solving service to get past the site's human-verification step,
then submits the booking form itself — typically within about a second of a slot appearing.

### 1. Give it your real details

```bash
cp applicant_data_real.json.example applicant_data_real.json
nano applicant_data_real.json    # fill in your real name, date of birth, phone, email, and
                                  # your visa application number (from your original application)
chmod 600 applicant_data_real.json
```

This file is never uploaded anywhere except directly to the visa site's own form, and it's
excluded from git so it can't accidentally end up in this repository.

### 2. Get a CAPTCHA-solving API key

Sign up at [capsolver.com](https://www.capsolver.com) and top up a small balance (a few
dollars lasts a long time at typical usage — CapSolver bills per solve, at a small fraction of
a cent each).

### 3. If you started on the free-tier server, resize it

The Booker is heavier than the Monitor (it may briefly use more CPU while working). If your VM
is still `e2-micro`, stop it and resize:

```bash
# Console → Compute Engine → your instance → Stop, then:
gcloud compute instances set-machine-type estonia-visa-monitor \
  --zone=us-central1-<your-zone> \
  --machine-type=e2-standard-2
# then Start it again
```

This takes it out of the free tier, so it starts incurring a small ongoing cost — check
current GCP pricing before committing. Everything else on the VM is untouched.

### 4. Add the Booker's secrets

```bash
cat >> run_monitor.sh << 'EOF'
export CAPSOLVER_API_KEY="your_capsolver_key"
export APPLICANT_DATA_REAL="/home/YOUR_USERNAME/estonia-d-visa-appointment/applicant_data_real.json"
export APPLICANT_DATA_DUMMY="/home/YOUR_USERNAME/estonia-d-visa-appointment/applicant_data_dummy.json"
EOF
```

Then extract just these secret lines into a second file the Booker reads from directly:

```bash
grep '^export ' run_monitor.sh > run_booker_daemon_env.sh
```

### 5. Install and start the Booker

```bash
source venv/bin/activate
pip install -r requirements.txt   # installs playwright too, used only by an older fallback booker
sudo cp estonia-booker-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now estonia-booker-daemon
sudo systemctl status estonia-booker-daemon   # confirm it's running
```

Set up log rotation for it too, same idea as the Monitor's:

```bash
sudo cp estonia-booker-daemon.logrotate /etc/logrotate.d/estonia-booker-daemon
```

From now on, the Booker runs continuously in the background, and restarts itself
automatically if it ever crashes.

### 6. Test it without risking a real booking

```bash
export DAEMON_DRY_RUN=1
venv/bin/python3 estonia_booker_daemon.py
```

This runs through the entire flow — solving a real CAPTCHA, reaching the booking screen —
but stops one step short of actually submitting, using the placeholder "John Doe" details in
`applicant_data_dummy.json` rather than your real ones.

### 7. If you're booking for two people

If you (or a family member) each need your own appointment, one Booker can hunt for both —
just point it at a different applicant's details and restart:

```bash
cp applicant_data_real_2.json.example applicant_data_real_2.json
nano applicant_data_real_2.json
chmod 600 applicant_data_real_2.json

./switch_daemon_applicant.sh applicant2   # switches the Booker to hunt/book for person 2
./switch_daemon_applicant.sh applicant1   # switches back to person 1
```

Only one hunt is ever active at a time — this doesn't run two Bookers in parallel, it just
repoints the same one. Once someone's real appointment is later cancelled and you want the
Booker to start hunting fresh for them again, add `--clear-state`:
`./switch_daemon_applicant.sh applicant1 --clear-state` (only do this once the real
appointment is actually cancelled — otherwise the Booker won't know to stop treating it as
already secured).

---

## Everyday commands

Run these from `~/estonia-d-visa-appointment` on the server.

```bash
./run_monitor.sh                        # run one check manually
TEST_TELEGRAM=true ./run_monitor.sh     # send a test alert without touching the real site
tail -f monitor.log                     # watch the Monitor's live log
sudo systemctl status estonia-booker-daemon      # is the Booker running?
journalctl -u estonia-booker-daemon -f           # watch the Booker's live log
cat alerts_history.log                  # permanent record of every real alert ever sent
crontab -l                              # see the Monitor's schedule
git pull                                # get the latest version of this code
```

---

## What alerts look like

| Event | Telegram | Phone call |
|---|---|---|
| Slot found | Every check, instantly | Up to 3 calls, then a cooldown before trying again |
| Slot found, and the Booker secured it | A ✅ success message | — |
| Slot found, but the Booker couldn't secure it | A message telling you to book by hand, fast | — |
| Nothing found | Silent | — |
| Something's broken | One alert, then it won't repeat for a while | Never — this isn't urgent enough to call about |

---

## Prefer not to run a server? Use GitHub Actions instead

This repo also ships a plain GitHub Actions workflow
(`.github/workflows/estonia-visa-monitor.yml`) that runs the Monitor on GitHub's own
infrastructure — no server to set up or pay for, but no Booker, and it checks only every 15
minutes instead of every 10 seconds (a real trade-off if slots close within minutes).

**It's off by default.** To turn it on:

1. In this repository's Settings → Secrets and variables → Actions, add two secrets:
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. Open `.github/workflows/estonia-visa-monitor.yml` and uncomment the schedule lines
   (instructions are right there in the file).
3. Or leave it off and just trigger a check manually anytime from this repo's **Actions** tab.

---

## Current limitations

- The visa site requires solving a "I'm not a robot" CAPTCHA at the actual booking step. The
  Booker handles this automatically via a paid solving service, but it does mean each real
  booking attempt costs a small fee (a fraction of a cent) rather than being entirely free.
- You need your own real applicant details entered in advance for the Booker to use — it
  can't book anything for you it doesn't already have your information for.
- Running the Booker costs a small monthly server fee, since it needs more than the
  permanently-free server tier.
- This has been tested against the New Delhi embassy and a couple of others, but the site
  could change its design at any time, which may require updating the code.
- To monitor a different embassy, set `EMBASSY_ID` and `EMBASSY_NAME` in `run_monitor.sh` —
  the code already supports this, no need to modify anything else.
