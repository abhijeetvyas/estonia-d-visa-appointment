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
export TWOCAPTCHA_API_KEY="your_2captcha_key"
export APPLICANT_DATA_REAL="/home/abhijeetvyas13/estonia-d-visa-appointment/applicant_data_real.json"
export APPLICANT_DATA_DUMMY="/home/abhijeetvyas13/estonia-d-visa-appointment/applicant_data_dummy.json"
# optional overrides
# export BOOKER_COOLDOWN_SECONDS=90
# export DRY_RUN=1          # for testing – never submits
# export FORCE_LIST_ONLY=1  # force Stage-2 only
```

### Point cron at the hook (instead of the original monitor)

Edit `run_monitor.sh` so the final Python line becomes:

```bash
/home/abhijeetvyas13/estonia-d-visa-appointment/venv/bin/python3 \
  estonia_visa_monitor_booker_hook.py >> monitor.log 2>&1
```

(The original `estonia_visa_monitor.py` remains available if you ever want to disable the booker by simply unsetting `ENABLE_BOOKER`.)

### Dry-run test on the VM

```bash
cd ~/estonia-d-visa-appointment
source venv/bin/activate
export DRY_RUN=1
export EMBASSY_ID=244          # Abu Dhabi has slots for testing
export EMBASSY_NAME="Abu Dhabi"
export TWOCAPTCHA_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python3 estonia_d_visa_booker.py
```

Screenshots land in `booker_screenshots/`. No real booking is performed.

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
