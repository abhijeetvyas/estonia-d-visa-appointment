#!/usr/bin/env python3
"""
Estonia D-visa appointment booker.

Triggered by the monitor when the "Long-stay visa (D-visa) application"
service appears in the dropdown. Implements the three-stage flow:

  Stage 1  – already handled by the lightweight monitor (Telegram + optional call)
  Stage 2  – reach calendar, collect every available date/time, send Telegram list
  Stage 3  – select preferred slot, solve 2nd reCAPTCHA, submit booking (only if
             the persistent "booked" flag is not set)

Modes (env):
  DRY_RUN=1          – use dummy applicant data, never click final submit,
                       take screenshots + HTML dumps
  FORCE_LIST_ONLY=1  – force Stage-2 only (ignore booked flag for testing)

Applicant data is loaded from JSON files that live only on the VM:
  APPLICANT_DATA_DUMMY  (default: applicant_data_dummy.json next to this script)
  APPLICANT_DATA_REAL   (default: applicant_data_real.json next to this script)

Concurrency:
  A lock file prevents multiple Playwright instances.  If the lock is held the
  booker exits early and sends a Telegram "skipped – already in progress" alert.
  After a run a short cooldown is recorded so the next monitor tick does not
  immediately re-launch.

Date preference: latest available date that is at least 3 days from today.
Time preference: earliest slot in 12:00–14:00; otherwise any available slot.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

URL = "https://broneering.mfa.ee/en/"
TARGET_SERVICE = "Long-stay visa (D-visa) application"
SERVICE_VALUE = "pikaajaline_viisa"  # stable backend value

EMBASSY_ID = os.environ.get("EMBASSY_ID", "40")
EMBASSY_NAME = os.environ.get("EMBASSY_NAME", "New Delhi")

# Known reCAPTCHA sitekey (identical across embassies)
RECAPTCHA_SITEKEY = "6Ld04R4nAAAAADJhUQpKyGxGh4zlfOsyk5VkVkzR"

SCRIPT_DIR = Path(__file__).resolve().parent
DUMMY_DATA_PATH = Path(os.environ.get("APPLICANT_DATA_DUMMY", SCRIPT_DIR / "applicant_data_dummy.json"))
REAL_DATA_PATH = Path(os.environ.get("APPLICANT_DATA_REAL", SCRIPT_DIR / "applicant_data_real.json"))

STATE_DIR = SCRIPT_DIR / ".state"
STATE_FILE = STATE_DIR / f"booker_state_{EMBASSY_ID}.json"
LOCK_FILE = Path(f"/tmp/estonia_d_visa_booker_{EMBASSY_ID}.lock")

# Cooldown after a booker run finishes (success or failure) before another
# launch is allowed.  Prevents stampeding when a slot stays open for minutes.
BOOKER_COOLDOWN_SECONDS = int(os.environ.get("BOOKER_COOLDOWN_SECONDS", "90"))

# How long the lock is considered stale (crash protection)
LOCK_STALE_SECONDS = 180

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
FORCE_LIST_ONLY = os.environ.get("FORCE_LIST_ONLY", "").lower() in ("1", "true", "yes")

SCREENSHOT_DIR = SCRIPT_DIR / "booker_screenshots"
if DRY_RUN:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

RUNTIME_SIGNATURE = "\n\n— Sent from GCP booker"


# ---------------------------------------------------------------------------
# Logging / Telegram helpers (mirror the monitor style)
# ---------------------------------------------------------------------------

def log_line(msg: str, file=sys.stdout) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} {msg}", file=file, flush=True)


def _telegram_creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("VISA_BOT")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("VISA_BOT_CHAT_ID")
    if not token or not chat_id_raw:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    chat_ids = [c.strip() for c in chat_id_raw.split(",") if c.strip()]
    if not chat_ids:
        raise RuntimeError("TELEGRAM_CHAT_ID is empty after parsing")
    return token, chat_ids


def send_telegram(message: str) -> None:
    token, chat_ids = _telegram_creds()
    errors = []
    sent = 0
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
                timeout=30,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
    if sent == 0:
        raise RuntimeError(f"Failed to send Telegram to all recipients: {'; '.join(errors)}")
    if errors:
        log_line(f"Telegram partial failure: {'; '.join(errors)}", file=sys.stderr)


# ---------------------------------------------------------------------------
# State / lock
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_already_booked() -> bool:
    return bool(load_state().get("booked"))


def mark_booked(details: dict) -> None:
    state = load_state()
    state["booked"] = True
    state["booked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["booking_details"] = details
    save_state(state)
    log_line(f"Persistent booked flag set: {details}")


def acquire_lock() -> bool:
    """Return True if we got the lock, False if another booker is running."""
    now = time.time()
    if LOCK_FILE.exists():
        try:
            age = now - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                return False
            log_line(f"Stale lock ({age:.0f}s old) – removing")
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n{now}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def under_cooldown() -> bool:
    state = load_state()
    last = state.get("last_booker_ts", 0)
    return (time.time() - last) < BOOKER_COOLDOWN_SECONDS


def record_booker_run() -> None:
    state = load_state()
    state["last_booker_ts"] = time.time()
    save_state(state)


# ---------------------------------------------------------------------------
# Applicant data
# ---------------------------------------------------------------------------

def load_applicant_data() -> dict:
    path = DUMMY_DATA_PATH if DRY_RUN else REAL_DATA_PATH
    if not path.exists():
        raise RuntimeError(
            f"Applicant data file not found: {path}\n"
            f"Create it on the VM (never commit real data to git)."
        )
    with open(path) as f:
        data = json.load(f)
    required = [
        "first_name", "surname", "date_of_birth", "phone",
        "email", "visa_application_number", "specifying_information",
    ]
    missing = [k for k in required if k not in data or not str(data[k]).strip()]
    if missing:
        raise RuntimeError(f"Applicant data missing keys: {missing}")
    return data


# ---------------------------------------------------------------------------
# reCAPTCHA (2captcha)
# ---------------------------------------------------------------------------

def solve_recaptcha(page_url: str, sitekey: str, timeout: int = 120) -> str:
    api_key = os.environ.get("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise RuntimeError("TWOCAPTCHA_API_KEY not set")

    log_line(f"Solving reCAPTCHA sitekey={sitekey[:12]}…")
    resp = requests.post(
        "http://2captcha.com/in.php",
        data={
            "key": api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != 1:
        raise RuntimeError(f"2captcha create task failed: {result}")
    captcha_id = result["request"]
    log_line(f"2captcha task id={captcha_id}")

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        poll = requests.get(
            "http://2captcha.com/res.php",
            params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            timeout=30,
        )
        poll.raise_for_status()
        data = poll.json()
        if data.get("status") == 1:
            log_line("2captcha solved")
            return data["request"]
        if data.get("request") == "CAPCHA_NOT_READY":
            continue
        raise RuntimeError(f"2captcha error: {data}")
    raise RuntimeError("2captcha timed out")


def inject_recaptcha(page, token: str) -> None:
    page.evaluate(
        """
        (token) => {
            const textareas = document.querySelectorAll('textarea.g-recaptcha-response');
            textareas.forEach(ta => {
                ta.value = token;
                ta.dispatchEvent(new Event('change', { bubbles: true }));
                ta.dispatchEvent(new Event('input', { bubbles: true }));
            });
            if (typeof grecaptcha !== 'undefined') {
                try { grecaptcha.getResponse = () => token; } catch (e) {}
            }
        }
        """,
        token,
    )


def get_recaptcha_sitekey(page) -> str | None:
    html = page.content()
    m = re.search(r'data-sitekey="([^"]+)"', html)
    if m:
        return m.group(1)
    try:
        return page.evaluate(
            """() => {
                const el = document.querySelector('.g-recaptcha');
                return el ? el.getAttribute('data-sitekey') : null;
            }"""
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

def screenshot(page, name: str) -> None:
    if not DRY_RUN:
        return
    path = SCREENSHOT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log_line(f"Screenshot: {path}")


def wait_settle(page, seconds: float = 2.0) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeout:
        pass
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Date / time preference logic
# ---------------------------------------------------------------------------

def parse_time(t: str) -> tuple[int, int]:
    """'10:15' → (10, 15)"""
    parts = t.strip().split(":")
    return int(parts[0]), int(parts[1])


def choose_date(available_dates: list[str]) -> str | None:
    """
    Prefer the latest date that is at least 3 days from today (UTC).
    Fallback to the earliest available date if none satisfy the rule.
    """
    if not available_dates:
        return None
    today = datetime.now(timezone.utc).date()
    min_date = today + timedelta(days=3)

    candidates = []
    for d in available_dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            candidates.append((dt, d))
        except ValueError:
            continue
    if not candidates:
        return None

    good = [(dt, s) for dt, s in candidates if dt >= min_date]
    if good:
        # latest among those ≥ 3 days away
        good.sort(key=lambda x: x[0], reverse=True)
        return good[0][1]

    # fallback: earliest available
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def choose_time(time_labels: list[str]) -> str | None:
    """
    Prefer earliest slot in 12:00–14:00.
    Otherwise take the earliest available slot overall.
    """
    if not time_labels:
        return None

    parsed = []
    for label in time_labels:
        try:
            h, m = parse_time(label)
            parsed.append((h, m, label))
        except Exception:
            continue
    if not parsed:
        return None

    midday = [(h, m, lab) for h, m, lab in parsed if 12 <= h < 14]
    if midday:
        midday.sort()
        return midday[0][2]

    parsed.sort()
    return parsed[0][2]


# ---------------------------------------------------------------------------
# Core booking flow
# ---------------------------------------------------------------------------

def run_booker() -> dict:
    applicant = load_applicant_data()
    list_only = FORCE_LIST_ONLY or is_already_booked()
    mode = "DRY_RUN" if DRY_RUN else ("LIST_ONLY" if list_only else "LIVE_BOOK")
    log_line(f"Booker starting mode={mode} embassy={EMBASSY_ID}/{EMBASSY_NAME}")

    result: dict[str, Any] = {
        "status": "unknown",
        "mode": mode,
        "embassy": EMBASSY_ID,
        "embassy_name": EMBASSY_NAME,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            # ----- Landing page -----
            log_line("Opening booking page")
            page.goto(URL, wait_until="networkidle", timeout=45000)
            screenshot(page, "01_landing")

            # Select embassy + persons
            page.select_option('select[name="broneering[esindus]"]', EMBASSY_ID)
            page.fill('input[name="broneering[isikuteArv]"]', "1")
            time.sleep(0.5)
            screenshot(page, "02_embassy_selected")

            # Turbo submit to load service dropdown
            page.evaluate("() => document.querySelector('button[name=\"turbo\"]').click()")
            wait_settle(page, 2.5)
            screenshot(page, "03_after_turbo")

            # Service dropdown
            service_sel = page.locator('select[name="broneering[toiming]"], select#broneering_toiming')
            if service_sel.count() == 0:
                raise RuntimeError("Service dropdown not found after embassy submit")

            options = [o.strip() for o in service_sel.locator("option").all_text_contents()]
            log_line(f"Service options: {options}")
            target_value = None
            for opt in service_sel.locator("option").all():
                txt = (opt.text_content() or "").strip()
                val = opt.get_attribute("value") or ""
                if TARGET_SERVICE in txt or txt == TARGET_SERVICE or val == SERVICE_VALUE:
                    target_value = val
                    break
            if not target_value:
                raise RuntimeError(f"Target service not present. Options: {options}")

            service_sel.select_option(target_value)
            time.sleep(0.5)
            screenshot(page, "04_service_selected")

            # ----- First reCAPTCHA -----
            sitekey1 = get_recaptcha_sitekey(page) or RECAPTCHA_SITEKEY
            token1 = solve_recaptcha(URL, sitekey1)
            inject_recaptcha(page, token1)
            time.sleep(1)
            screenshot(page, "05_captcha1_injected")

            page.click('button[name="edasi"]')
            wait_settle(page, 3)
            screenshot(page, "06_applicant_page")

            # ----- Applicant form -----
            log_line("Filling applicant details")
            page.fill('input[name="broneering[broneeringuIsikud][0][eesnimi]"]', applicant["first_name"])
            page.fill('input[name="broneering[broneeringuIsikud][0][perenimi]"]', applicant["surname"])
            page.fill('input[name="broneering[broneeringuIsikud][0][synniaeg]"]', applicant["date_of_birth"])
            page.fill('input[name="broneering[broneeringuIsikud][0][telefon]"]', applicant["phone"])
            page.fill('input[name="broneering[broneeringuIsikud][0][epost]"]', applicant["email"])
            page.fill(
                'input[name="broneering[broneeringuIsikud][0][viisataotluseNumber]"]',
                applicant["visa_application_number"],
            )
            time.sleep(0.5)
            screenshot(page, "07_applicant_filled")

            page.click('button[name="edasi"]')
            wait_settle(page, 3)
            screenshot(page, "08_calendar_page")

            if DRY_RUN:
                html_path = SCREENSHOT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_08_calendar.html"
                html_path.write_text(page.content(), encoding="utf-8")
                log_line(f"Calendar HTML: {html_path}")

            # ----- Extract calendar data -----
            calendar_data = page.evaluate(
                """() => {
                    const el = document.querySelector('[data-controller="broneering"]');
                    if (!el) return null;
                    return {
                        vastuvotukuupaevad: el.getAttribute('data-vastuvotukuupaevad'),
                        riigipyhad: el.getAttribute('data-riigipyhad'),
                        etteBron: el.getAttribute('data-etteBron'),
                        isikuteArv: el.getAttribute('data-isikute-arv'),
                    };
                }"""
            )
            log_line(f"Calendar data attrs: {calendar_data}")

            available_dates: list[str] = []
            if calendar_data and calendar_data.get("vastuvotukuupaevad"):
                try:
                    available_dates = json.loads(calendar_data["vastuvotukuupaevad"])
                except Exception:
                    pass
            log_line(f"Available dates: {available_dates}")

            # Collect every time slot that appears for each date (we only need the
            # full list for the Stage-2 Telegram; actual selection happens later).
            # For the message we report the dates; individual times are discovered
            # after clicking a date.
            stage2_msg = (
                f"📅 Available D-visa slots – Embassy of Estonia in {EMBASSY_NAME}\n\n"
                f"Dates found: {', '.join(available_dates) if available_dates else '(none)'}\n\n"
                f"(Time slots appear after a date is selected; the booker will choose "
                f"according to preference rules.)\n"
                f"Book here: {URL}"
                f"{RUNTIME_SIGNATURE}"
            )
            try:
                send_telegram(stage2_msg)
                log_line("Stage-2 Telegram sent")
            except Exception as e:
                log_line(f"Stage-2 Telegram failed: {e}", file=sys.stderr)

            result["available_dates"] = available_dates
            result["stage2_sent"] = True

            if not available_dates:
                result["status"] = "no_dates"
                result["message"] = "Reached calendar but no available dates"
                return result

            # ----- Choose date -----
            selected_date = choose_date(available_dates)
            if not selected_date:
                result["status"] = "no_suitable_date"
                result["message"] = "Could not choose a date from available list"
                return result
            log_line(f"Chosen date: {selected_date}")

            # Click the day in flatpickr, ensuring it triggers even if previously selected
            day_sel = f'.flatpickr-day[aria-label="{selected_date}"]'
            if page.locator(day_sel).count() > 0:
                page.evaluate(
                    f"""() => {{
                        const day = document.querySelector('.flatpickr-day[aria-label="{selected_date}"]');
                        if (day) {{
                            day.classList.add('selected');
                            day.click();
                        }}
                    }}"""
                )
            else:
                page.evaluate(
                    f"""() => {{
                        const days = document.querySelectorAll('.flatpickr-day');
                        for (const day of days) {{
                            if (day.getAttribute('aria-label') === '{selected_date}') {{
                                day.click(); break;
                            }}
                        }}
                    }}"""
                )
            time.sleep(1)

            # Also set the hidden date input (required by Stimulus)
            page.evaluate(
                f"""() => {{
                    const input = document.querySelector('input[name="broneering[kuupaev]"]');
                    if (input) {{
                        input.value = '{selected_date}';
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                }}"""
            )
            
            # Wait for time slots to load asynchronously after date selection
            try:
                page.wait_for_selector('input[type="checkbox"][name="broneering[vastuvotud][]"]', timeout=10000)
            except PlaywrightTimeout:
                log_line("Timeout waiting for time slot checkboxes to render", file=sys.stderr)
            
            wait_settle(page, 2)
            screenshot(page, "09_date_selected")

            # ----- Collect time slots for the chosen date -----
            time_checkboxes = page.locator('input[type="checkbox"][name="broneering[vastuvotud][]"]')
            # Also try to read visible labels
            time_labels = page.evaluate(
                """() => {
                    const labels = [];
                    document.querySelectorAll('input[name="broneering[vastuvotud][]"]').forEach(cb => {
                        const lab = cb.parentElement ? cb.parentElement.textContent.trim() : '';
                        // often the label is a sibling or the value itself
                        const val = cb.value || lab;
                        if (val) labels.push(val);
                    });
                    // also grab any visible time text near checkboxes
                    document.querySelectorAll('label, span, div').forEach(el => {
                        const t = el.textContent.trim();
                        if (/^\\d{1,2}:\\d{2}$/.test(t)) labels.push(t);
                    });
                    return [...new Set(labels)];
                }"""
            )
            # Prefer clean HH:MM strings
            clean_times = [t for t in time_labels if re.match(r"^\d{1,2}:\d{2}$", t)]
            if not clean_times:
                # fallback: read the value attributes of the checkboxes
                clean_times = page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                        'input[name="broneering[vastuvotud][]"]')).map(c => c.value).filter(Boolean)"""
                )
            log_line(f"Time slots for {selected_date}: {clean_times}")

            selected_time = choose_time(clean_times)
            if not selected_time:
                # last resort: just check the first checkbox
                if time_checkboxes.count() > 0:
                    time_checkboxes.first.check()
                    selected_time = "(first available)"
                    log_line("No parseable times – checked first checkbox")
                else:
                    result["status"] = "no_time_slots"
                    result["message"] = f"No time slots found for {selected_date}"
                    return result
            else:
                # check the matching checkbox
                checked = page.evaluate(
                    f"""(wanted) => {{
                        const boxes = document.querySelectorAll('input[name="broneering[vastuvotud][]"]');
                        for (const cb of boxes) {{
                            const lab = (cb.parentElement && cb.parentElement.textContent) || '';
                            if (cb.value === wanted || lab.includes(wanted)) {{
                                cb.checked = true;
                                cb.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                return true;
                            }}
                        }}
                        // fallback first box
                        if (boxes.length) {{
                            boxes[0].checked = true;
                            boxes[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return 'first';
                        }}
                        return false;
                    }}""",
                    selected_time,
                )
                log_line(f"Time selection result: {checked} (wanted {selected_time})")
            screenshot(page, "10_time_selected")

            # Specifying information
            page.fill('textarea[name="broneering[markused]"]', applicant["specifying_information"])
            time.sleep(0.5)
            screenshot(page, "11_spec_filled")

            # ----- Second reCAPTCHA -----
            time.sleep(2)  # give async reCAPTCHA a moment
            sitekey2 = get_recaptcha_sitekey(page) or RECAPTCHA_SITEKEY
            token2 = solve_recaptcha(URL, sitekey2)
            inject_recaptcha(page, token2)
            time.sleep(1)
            screenshot(page, "12_captcha2_injected")

            result["selected_date"] = selected_date
            result["selected_time"] = selected_time
            result["captcha1"] = True
            result["captcha2"] = True

            # ----- Final decision -----
            if list_only or DRY_RUN:
                log_line("Stopping before final submit (list-only or dry-run)")
                result["status"] = "list_only_or_dryrun"
                result["message"] = (
                    f"Reached final page. Would have booked {selected_date} {selected_time}. "
                    f"Submit skipped (mode={mode})."
                )
                return result

            # LIVE submit
            log_line("Clicking final 'Book the appointment' (broneeri)")
            # button can be name=broneeri or type=submit with that text
            submit = page.locator('button[name="broneeri"], button:has-text("Book the appointment")')
            if submit.count() == 0:
                # last-chance
                submit = page.locator('button[type="submit"]').last
            submit.first.click()
            wait_settle(page, 4)
            screenshot(page, "13_after_submit")

            # Heuristic success check
            page_text = page.inner_text("body")
            confirmation_markers = [
                "Booked time",
                "Add new booking",
                "Delete",
                selected_date.replace("-", "."),  # 18.08.2026 style
            ]
            success = any(m in page_text for m in confirmation_markers)

            if success:
                details = {
                    "date": selected_date,
                    "time": selected_time,
                    "embassy": EMBASSY_NAME,
                }
                mark_booked(details)
                msg = (
                    f"✅ BOOKED – Estonia D-visa appointment\n\n"
                    f"Embassy: {EMBASSY_NAME}\n"
                    f"Date: {selected_date}\n"
                    f"Time: {selected_time}\n\n"
                    f"Check your email / the booking portal for confirmation."
                    f"{RUNTIME_SIGNATURE}"
                )
                send_telegram(msg)
                result["status"] = "booked"
                result["message"] = f"Successfully booked {selected_date} {selected_time}"
                log_line(result["message"])
            else:
                result["status"] = "submit_uncertain"
                result["message"] = (
                    "Final submit clicked but confirmation markers not found. "
                    "Check screenshots / portal manually."
                )
                log_line(result["message"], file=sys.stderr)
                try:
                    send_telegram(
                        f"⚠️ Booker submitted but confirmation unclear for {EMBASSY_NAME}.\n"
                        f"Selected: {selected_date} {selected_time}\n"
                        f"Please verify manually on the portal."
                        f"{RUNTIME_SIGNATURE}"
                    )
                except Exception:
                    pass

            return result

        except Exception as e:
            log_line(f"BOOKER ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
            screenshot(page, "99_error")
            result["status"] = "error"
            result["message"] = f"{type(e).__name__}: {e}"
            try:
                send_telegram(
                    f"⚠️ Estonia D-visa booker FAILED ({EMBASSY_NAME})\n\n"
                    f"Error: {type(e).__name__}: {e}\n\n"
                    f"Check booker logs on the VM."
                    f"{RUNTIME_SIGNATURE}"
                )
            except Exception as te:
                log_line(f"Could not send failure Telegram: {te}", file=sys.stderr)
            return result
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    # Concurrency / cooldown gates
    if under_cooldown():
        remaining = int(BOOKER_COOLDOWN_SECONDS - (time.time() - load_state().get("last_booker_ts", 0)))
        log_line(f"Booker under cooldown ({remaining}s remaining) – exiting")
        try:
            send_telegram(
                f"ℹ️ Booker skipped – cooldown active ({remaining}s left) "
                f"for {EMBASSY_NAME}."
                f"{RUNTIME_SIGNATURE}"
            )
        except Exception:
            pass
        return 0

    if not acquire_lock():
        log_line("Booker lock held by another process – skipping")
        try:
            send_telegram(
                f"ℹ️ Booker skipped – another booker instance is already in progress "
                f"({EMBASSY_NAME})."
                f"{RUNTIME_SIGNATURE}"
            )
        except Exception:
            pass
        return 0

    try:
        result = run_booker()
        record_booker_run()
        log_line(f"Booker finished: {json.dumps(result, default=str)}")
        return 0 if result.get("status") in ("booked", "list_only_or_dryrun", "no_dates") else 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
