import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

URL = 'https://broneering.mfa.ee/en/'
TARGET = 'Long-stay visa (D-visa) application'


def log_line(msg, file=sys.stdout):
    """Prints with an ISO timestamp prefix so log entries can be correlated
    with actual wall-clock time (monitor.log had no timestamps before this,
    making it impossible to tell when any given run happened)."""
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    print(f'{ts} {msg}', file=file)


# Separate, append-only file that ONLY ever records AVAILABLE/FAILURE events
# (never the routine "not available" noise). This file should never be
# manually truncated (unlike monitor.log, which is fine to clear/rotate) —
# it exists specifically so a real slot-found or failure event can't be lost
# to a `> monitor.log` reset during future debugging.
ALERTS_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'alerts_history.log')


def record_alert(msg):
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with open(ALERTS_LOG_FILE, 'a') as f:
        f.write(f'{ts} {msg}\n')

# Which embassy to check. Defaults preserve the original New Delhi behavior;
# pass EMBASSY_ID/EMBASSY_NAME env vars (wired from workflow_dispatch inputs
# in the workflow file) to reuse this same repo/script for another embassy
# instead of forking or branching.
EMBASSY_ID = os.environ.get('EMBASSY_ID', '40')
EMBASSY_NAME = os.environ.get('EMBASSY_NAME', 'New Delhi')

# Where GitHub Actions cache restores/saves failure-alert state between runs.
# Namespaced per embassy so two embassies checked from the same repo don't
# share (and incorrectly suppress) each other's failure-alert cooldown.
# See .github/workflows/estonia-visa-monitor.yml — this path is cached so
# repeated failures don't spam Telegram every 15 minutes.
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.state', f'monitor_state_{EMBASSY_ID}.json'
)
FAILURE_ALERT_COOLDOWN_SECONDS = int(1.5 * 60 * 60)  # re-alert on an ongoing failure at most every 1.5h


def check_availability():
    s = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept': 'text/html, application/xhtml+xml, application/xml;q=0.9, */*;q=0.8',
    }
    r = s.get(URL, headers=headers, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, 'html.parser')
    token_inp = soup.find('input', {'name': 'broneering[_token]'})
    if not token_inp:
        raise RuntimeError('CSRF token not found on landing page — site markup may have changed')
    token = token_inp.get('value')

    form = soup.find('form')
    if not form:
        raise RuntimeError('No <form> found on landing page — site markup may have changed')
    action = form.get('action') if form.get('action') else URL

    post_headers = {
        'User-Agent': headers['User-Agent'],
        'Accept': 'text/vnd.turbo-stream.html, text/html, application/xhtml+xml',
        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': URL,
        'Origin': 'https://broneering.mfa.ee',
    }
    data = {
        'broneering[esindus]': EMBASSY_ID,
        'broneering[isikuteArv]': '1',
        'broneering[_token]': token,
        'turbo': '',
    }
    r2 = s.post(action, headers=post_headers, data=data, timeout=30)
    r2.raise_for_status()

    soup2 = BeautifulSoup(r2.text, 'html.parser')
    sel = soup2.find('select', {'name': 'broneering[toiming]'}) or soup2.find('select', {'id': 'broneering_toiming'})
    if not sel:
        raise RuntimeError('Service dropdown not found in step-2 response — site markup may have changed')

    options = []
    for opt in sel.find_all('option'):
        val = opt.get('value', '').strip()
        txt = opt.get_text(strip=True)
        if val:
            options.append((val, txt))

    if not options:
        raise RuntimeError('Service dropdown was found but contained no options — treat as a possible parsing failure, not "no slots"')

    available = [txt for _, txt in options]
    found = any(txt == TARGET or TARGET in txt for txt in available)
    return found, available


def _telegram_creds():
    token = os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('VISA_BOT')
    chat_id_raw = os.environ.get('TELEGRAM_CHAT_ID') or os.environ.get('VISA_BOT_CHAT_ID')
    if not token or not chat_id_raw:
        raise RuntimeError('TELEGRAM_BOT_TOKEN/VISA_BOT or TELEGRAM_CHAT_ID/VISA_BOT_CHAT_ID not set')
    # Supports multiple recipients: TELEGRAM_CHAT_ID="111111,222222"
    chat_ids = [c.strip() for c in chat_id_raw.split(',') if c.strip()]
    if not chat_ids:
        raise RuntimeError('TELEGRAM_CHAT_ID/VISA_BOT_CHAT_ID is set but empty after parsing')
    return token, chat_ids


def send_telegram_text(message):
    token, chat_ids = _telegram_creds()
    errors = []
    sent_count = 0
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                data={'chat_id': chat_id, 'text': message},
                timeout=30,
            )
            resp.raise_for_status()
            sent_count += 1
        except Exception as e:
            errors.append(f'{chat_id}: {e}')

    if sent_count == 0:
        # Every recipient failed — this should count as a real send failure.
        raise RuntimeError(f'Failed to send to all Telegram recipients: {"; ".join(errors)}')
    if errors:
        # Partial failure: at least one recipient got it, so don't crash the
        # whole run over it, but make sure it's visible in the logs.
        print(f'Telegram send partially failed for some recipients: {"; ".join(errors)}', file=sys.stderr)


RUNTIME_SIGNATURE = f"\n\n— Sent from {os.environ.get('RUNTIME_LABEL', 'GCP')}"

# Since the site is checked frequently, a slot staying open for a while
# would otherwise trigger a phone call every single check — pointless once
# you've missed the first couple. Telegram still fires every check,
# uncapped. Calls are spaced out and capped per "episode": up to
# MAX_CALLS_PER_ESCALATION calls, each at least CALL_COOLDOWN_SECONDS apart
# (so 3 calls across a 6-minute span at 2-minute spacing). Once that cap is
# hit, calling pauses for CALL_ESCALATION_COOLDOWN_SECONDS before a fresh
# cycle of up to 3 more calls can start (still only if the slot is available
# again by then).
CALL_COOLDOWN_SECONDS = 2 * 60
MAX_CALLS_PER_ESCALATION = 3
CALL_ESCALATION_COOLDOWN_SECONDS = 5 * 60


def trigger_phone_calls(bypass_cooldown=False):
    """Places a Twilio voice call to each configured recipient. Used only for
    real availability alerts and test runs — never for failure alerts, by
    design (failures are frequent/expected during outages and shouldn't ring
    anyone's phone repeatedly)."""
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_number = os.environ.get('TWILIO_FROM_NUMBER')
    to_numbers_raw = os.environ.get('TWILIO_TO_NUMBERS')
    twiml_url = os.environ.get('TWILIO_TWIML_URL')

    if not all([account_sid, auth_token, from_number, to_numbers_raw, twiml_url]):
        print(
            'Twilio call skipped: one or more of TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/'
            'TWILIO_FROM_NUMBER/TWILIO_TO_NUMBERS/TWILIO_TWIML_URL not set.',
            file=sys.stderr,
        )
        return

    state = None
    if not bypass_cooldown:
        state = load_state()
        last_call = state.get('last_call_ts', 0)
        call_count = state.get('call_count', 0)
        now = time.time()

        # After hitting the per-episode cap, the required gap before another
        # call is the long escalation cooldown instead of the normal 5 min.
        required_gap = (
            CALL_ESCALATION_COOLDOWN_SECONDS if call_count >= MAX_CALLS_PER_ESCALATION else CALL_COOLDOWN_SECONDS
        )

        if now - last_call < required_gap:
            remaining = int(required_gap - (now - last_call))
            log_line(
                f'Twilio call skipped (cooldown active, {remaining}s remaining, '
                f'{call_count}/{MAX_CALLS_PER_ESCALATION} calls made this episode). '
                f'Telegram alert still sent.'
            )
            return

        # Cooldown has passed. If we'd hit the cap, this call starts a fresh
        # episode (reset the count) rather than continuing the old one.
        if call_count >= MAX_CALLS_PER_ESCALATION:
            call_count = 0

        state['last_call_ts'] = now
        state['call_count'] = call_count + 1
        save_state(state)

    to_numbers = [n.strip() for n in to_numbers_raw.split(',') if n.strip()]
    errors = []
    for to_number in to_numbers:
        try:
            resp = requests.post(
                f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json',
                auth=(account_sid, auth_token),
                data={'To': to_number, 'From': from_number, 'Url': twiml_url},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as e:
            errors.append(f'{to_number}: {e}')

    if errors:
        # Don't let a failed call crash the run — Telegram already went out
        # (or is about to) as the primary channel; the call is a bonus.
        print(f'Twilio call failed for some recipients: {"; ".join(errors)}', file=sys.stderr)
    else:
        print(f'Twilio calls placed to: {", ".join(to_numbers)}')


def _cooldown_hours_display():
    hours = FAILURE_ALERT_COOLDOWN_SECONDS / 3600
    return f'{hours:g}h'


def send_availability_alert(available):
    services_text = '\n'.join(f'- {s}' for s in available) if available else '- (none listed)'
    message = (
        f'✅ Long-stay visa (D-visa) application is now available for '
        f'Embassy of the Republic of Estonia in {EMBASSY_NAME}.\n\n'
        f'Available services:\n{services_text}\n\n'
        f'Book here: {URL}'
        f'{RUNTIME_SIGNATURE}'
    )
    send_telegram_text(message)
    trigger_phone_calls()
    record_alert(f'AVAILABLE: {available}')


def send_failure_alert(error_summary):
    message = (
        f'⚠️ Estonia visa monitor ({EMBASSY_NAME}) is FAILING and cannot currently check the '
        f'booking calendar.\n\n'
        f'Error: {error_summary}\n\n'
        f'This means slot checks are not happening reliably until this is '
        f"fixed. Check monitor.log on {os.environ.get('RUNTIME_LABEL', 'the VM')} for the full traceback.\n"
        f'(You will not be re-alerted for the same ongoing issue for '
        f'{_cooldown_hours_display()}.)'
        f'{RUNTIME_SIGNATURE}'
    )
    send_telegram_text(message)


def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def handle_failure(exc):
    """Best-effort alert on failure, with a cooldown so an ongoing outage
    doesn't spam Telegram every 15 minutes. Never raises further — a failure
    in the alerting path must not mask the original error's exit code."""
    error_summary = f'{type(exc).__name__}: {exc}'
    log_line(f'MONITOR FAILURE: {error_summary}', file=sys.stderr)
    traceback.print_exc()
    record_alert(f'FAILURE: {error_summary}')

    state = load_state()
    last_alert = state.get('last_failure_alert_ts', 0)
    now = time.time()

    if now - last_alert >= FAILURE_ALERT_COOLDOWN_SECONDS:
        try:
            send_failure_alert(error_summary)
            log_line('Failure alert sent to Telegram.', file=sys.stderr)
            state['last_failure_alert_ts'] = now
            save_state(state)
        except Exception as alert_exc:
            # If Telegram itself is unreachable/misconfigured, we can't alert —
            # rely on the GitHub Actions run showing as failed/red instead.
            log_line(f'Could not send failure alert to Telegram: {alert_exc}', file=sys.stderr)
    else:
        remaining = int(FAILURE_ALERT_COOLDOWN_SECONDS - (now - last_alert))
        log_line(f'Suppressing duplicate failure alert (cooldown active, {remaining}s remaining).', file=sys.stderr)


def clear_failure_state_if_needed():
    """Once a check succeeds again, reset the cooldown so the *next* failure
    (a fresh, different issue) alerts immediately instead of waiting out an
    old cooldown window."""
    state = load_state()
    if state.get('last_failure_alert_ts'):
        state['last_failure_alert_ts'] = 0
        save_state(state)


def send_test_alert():
    """Dummy run that only exercises the Telegram send path — does not touch
    the booking site at all. Used to verify TELEGRAM_BOT_TOKEN/CHAT_ID are
    wired correctly in GitHub Actions before relying on the real monitor."""
    message = (
        f'🧪 Test message from the Estonia D-visa slot monitor.\n\n'
        f'If you can read this, GitHub Actions can successfully send '
        f'Telegram messages using your configured secrets. No real '
        f'availability check was performed.'
        f'{RUNTIME_SIGNATURE}'
    )
    send_telegram_text(message)
    log_line('Test message sent to Telegram successfully.')
    trigger_phone_calls(bypass_cooldown=True)


def main():
    if os.environ.get('TEST_TELEGRAM', '').lower() in ('1', 'true', 'yes'):
        send_test_alert()
        return

    try:
        # One retry for transient network/session blips before treating this
        # as a real failure worth alerting on.
        try:
            found, available = check_availability()
        except Exception as e:
            log_line(f'First attempt failed ({e}), retrying once...', file=sys.stderr)
            found, available = check_availability()
    except Exception as exc:
        handle_failure(exc)
        sys.exit(1)  # mark the GitHub Actions run as failed too

    clear_failure_state_if_needed()

    if found:
        send_availability_alert(available)
        log_line(f'AVAILABLE: {TARGET}. Telegram sent. Services: {available}')
    else:
        log_line(f'NOT available. Services: {available}')


if __name__ == '__main__':
    main()
