import json
import os
import sys
import time
import traceback

import requests
from bs4 import BeautifulSoup

URL = 'https://broneering.mfa.ee/en/'
TARGET = 'Long-stay visa (D-visa) application'

# Where GitHub Actions cache restores/saves failure-alert state between runs.
# See .github/workflows/estonia-visa-monitor.yml — this path is cached so
# repeated failures don't spam Telegram every 15 minutes.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.state', 'monitor_state.json')
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
        'broneering[esindus]': '40',
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
    chat_id = os.environ.get('TELEGRAM_CHAT_ID') or os.environ.get('VISA_BOT_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('TELEGRAM_BOT_TOKEN/VISA_BOT or TELEGRAM_CHAT_ID/VISA_BOT_CHAT_ID not set')
    return token, chat_id


def send_telegram_text(message):
    token, chat_id = _telegram_creds()
    resp = requests.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data={'chat_id': chat_id, 'text': message},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


GITHUB_SIGNATURE = '\n\n— Sent from GitHub Actions'


def _cooldown_hours_display():
    hours = FAILURE_ALERT_COOLDOWN_SECONDS / 3600
    return f'{hours:g}h'


def send_availability_alert(available):
    services_text = '\n'.join(f'- {s}' for s in available) if available else '- (none listed)'
    message = (
        f'✅ Long-stay visa (D-visa) application is now available for '
        f'Embassy of the Republic of Estonia in New Delhi.\n\n'
        f'Available services:\n{services_text}\n\n'
        f'Book here: {URL}'
        f'{GITHUB_SIGNATURE}'
    )
    send_telegram_text(message)


def send_failure_alert(error_summary):
    message = (
        f'⚠️ Estonia visa monitor is FAILING and cannot currently check the '
        f'booking calendar.\n\n'
        f'Error: {error_summary}\n\n'
        f'This means slot checks are not happening reliably until this is '
        f'fixed. Check the GitHub Actions run logs for the full traceback.\n'
        f'(You will not be re-alerted for the same ongoing issue for '
        f'{_cooldown_hours_display()}.)'
        f'{GITHUB_SIGNATURE}'
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
    print(f'MONITOR FAILURE: {error_summary}', file=sys.stderr)
    traceback.print_exc()

    state = load_state()
    last_alert = state.get('last_failure_alert_ts', 0)
    now = time.time()

    if now - last_alert >= FAILURE_ALERT_COOLDOWN_SECONDS:
        try:
            send_failure_alert(error_summary)
            print('Failure alert sent to Telegram.', file=sys.stderr)
            state['last_failure_alert_ts'] = now
            save_state(state)
        except Exception as alert_exc:
            # If Telegram itself is unreachable/misconfigured, we can't alert —
            # rely on the GitHub Actions run showing as failed/red instead.
            print(f'Could not send failure alert to Telegram: {alert_exc}', file=sys.stderr)
    else:
        remaining = int(FAILURE_ALERT_COOLDOWN_SECONDS - (now - last_alert))
        print(f'Suppressing duplicate failure alert (cooldown active, {remaining}s remaining).', file=sys.stderr)


def clear_failure_state_if_needed():
    """Once a check succeeds again, reset the cooldown so the *next* failure
    (a fresh, different issue) alerts immediately instead of waiting out an
    old cooldown window."""
    state = load_state()
    if state.get('last_failure_alert_ts'):
        state['last_failure_alert_ts'] = 0
        save_state(state)


def main():
    try:
        # One retry for transient network/session blips before treating this
        # as a real failure worth alerting on.
        try:
            found, available = check_availability()
        except Exception as e:
            print(f'First attempt failed ({e}), retrying once...', file=sys.stderr)
            found, available = check_availability()
    except Exception as exc:
        handle_failure(exc)
        sys.exit(1)  # mark the GitHub Actions run as failed too

    clear_failure_state_if_needed()

    if found:
        send_availability_alert(available)
        print(f'AVAILABLE: {TARGET}. Telegram sent. Services: {available}')
    else:
        print(f'NOT available. Services: {available}')


if __name__ == '__main__':
    main()
