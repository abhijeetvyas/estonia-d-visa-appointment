#!/usr/bin/env python3
"""
Establishes ONE booking session, parks it at the calendar, and then polls real
slot inventory on a timer to answer two questions that decide the booker's
whole architecture.

    Q1. How long does a parked session stay usable?
    Q2. Does real slot inventory churn (appear/disappear) while we watch it?

WHY THIS MATTERS
----------------
probe_http_flow.py established two facts on 2026-08-02:

  * Both reCAPTCHAs are enforced, and a solve costs 15-25s. That is now ~90%
    of the booker's critical path; all the actual HTTP work totals ~2.5s.
  * The date/slot query needs NO CAPTCHA and takes 0.21s.

Those two together imply something useful: once a session is past the CAPTCHA
gate, real slot inventory can be polled indefinitely for free. If a session
survives for hours, the booker stops being "detect, then spend 20s clawing
through a form" and becomes "already sitting on the calendar, submit
instantly". It also replaces the service-dropdown proxy with ground truth -
directly addressing why 5/5 real triggers reported `no_time_slots` while the
calendar simultaneously claimed the date was available.

If sessions die in minutes, that architecture is off the table and the answer
is a pre-solved CAPTCHA token reserve instead. Either way this test decides
it, for the price of a single CAPTCHA solve.

Q2 matters independently. Abu Dhabi has standing availability, so watching
whether its slots blink in and out tests the "a competing bot holds and
releases the slot" hypothesis on an embassy we can observe right now, instead
of waiting for the next New Delhi window.

SAFETY
------
This script CANNOT book anything. There is no `broneeri` POST anywhere in this
file - the only POSTs it makes after setup are date-selection queries, which
are what the calendar page itself sends when you click a date. It never checks
a time-slot checkbox and never submits. Read-only by construction, not by a
flag that could be set wrong.

USAGE
-----
    export REQUESTS_CA_BUNDLE=/home/abhijeetvyas13/estonia-d-visa-appointment/combined_ca_bundle.pem
    export CAPSOLVER_API_KEY="..."

    # Default: Abu Dhabi, poll every 15s for 1 hour
    venv/bin/python3 test_session_longevity.py

    # Longer / different cadence
    DURATION_SECONDS=10800 POLL_INTERVAL_SECONDS=30 venv/bin/python3 test_session_longevity.py

Writes a CSV alongside the log for easy analysis.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

URL = "https://broneering.mfa.ee/en/"
SERVICE_VALUE = "pikaajaline_viisa"
RECAPTCHA_SITEKEY = "6Ld04R4nAAAAADJhUQpKyGxGh4zlfOsyk5VkVkzR"

EMBASSY_ID = os.environ.get("EMBASSY_ID", "244")
EMBASSY_NAME = os.environ.get("EMBASSY_NAME", "Abu Dhabi")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
DURATION_SECONDS = int(os.environ.get("DURATION_SECONDS", "3600"))

SCRIPT_DIR = Path(__file__).resolve().parent
DUMMY_DATA_PATH = Path(os.environ.get("APPLICANT_DATA_DUMMY", SCRIPT_DIR / "applicant_data_dummy.json"))
OUT_DIR = SCRIPT_DIR / "session_tests"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_t0 = time.time()


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} t+{time.time() - _t0:8.1f}s  {msg}", flush=True)


def extract_token(html: str) -> str | None:
    soup = BeautifulSoup(html, HTML_PARSER)
    inp = soup.find("input", {"name": "broneering[_token]"})
    if inp and inp.get("value"):
        return inp["value"]
    m = re.search(r'name="broneering\[_token\]"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def solve_capsolver(timeout: int = 120) -> str:
    api_key = os.environ.get("CAPSOLVER_API_KEY")
    if not api_key:
        raise RuntimeError("CAPSOLVER_API_KEY not set")
    r = requests.post(
        "https://api.capsolver.com/createTask",
        json={
            "clientKey": api_key,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": URL,
                "websiteKey": RECAPTCHA_SITEKEY,
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errorId"):
        raise RuntimeError(f"CapSolver create failed: {payload}")
    task_id = payload["taskId"]
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(3)
        p = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        )
        p.raise_for_status()
        d = p.json()
        if d.get("errorId"):
            raise RuntimeError(f"CapSolver error: {d}")
        if d.get("status") == "ready":
            log(f"CAPTCHA solved in {time.time() - start:.1f}s (the only one this test will buy)")
            return d["solution"]["gRecaptchaResponse"]
    raise RuntimeError("CapSolver timed out")


def main() -> int:
    applicant = json.loads(DUMMY_DATA_PATH.read_text())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"session_longevity_{EMBASSY_ID}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    turbo_headers = {
        "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": URL,
        "Origin": "https://broneering.mfa.ee",
    }

    log(f"Establishing session for {EMBASSY_NAME} ({EMBASSY_ID})")

    r = s.get(URL, timeout=30)
    r.raise_for_status()
    token = extract_token(r.text)
    if not token:
        log("FAIL: no CSRF token on landing page")
        return 1

    r = s.post(
        URL,
        headers=turbo_headers,
        data={
            "broneering[esindus]": EMBASSY_ID,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": "",
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "turbo": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, HTML_PARSER)
    sel = soup.find("select", {"name": "broneering[toiming]"}) or soup.find("select", {"id": "broneering_toiming"})
    services = [o.get_text(strip=True) for o in sel.find_all("option")] if sel else []
    if not any("D-visa" in x for x in services):
        log(f"STOP: {EMBASSY_NAME} is not currently offering the D-visa service ({services})")
        log("      Point this at an embassy that is (Abu Dhabi 244 has standing availability).")
        return 0
    token = extract_token(r.text) or token

    r = s.post(
        URL,
        headers=turbo_headers,
        data={
            "broneering[esindus]": EMBASSY_ID,
            "broneering[toiming]": SERVICE_VALUE,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": "",
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "turbo": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    token = extract_token(r.text) or token

    captcha = solve_capsolver()
    r = s.post(
        URL,
        headers={**turbo_headers, "Accept": "text/html, application/xhtml+xml"},
        data={
            "broneering[esindus]": EMBASSY_ID,
            "broneering[toiming]": SERVICE_VALUE,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": captcha,
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "edasi": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, HTML_PARSER)
    if not soup.find("input", {"name": "broneering[broneeringuIsikud][0][eesnimi]"}):
        log("FAIL: CAPTCHA gate rejected us; cannot establish a session")
        return 1
    token = extract_token(r.text) or token

    r = s.post(
        URL,
        headers={**turbo_headers, "Accept": "text/html, application/xhtml+xml"},
        data={
            "broneering[broneeringuIsikud][0][eesnimi]": applicant["first_name"],
            "broneering[broneeringuIsikud][0][perenimi]": applicant["surname"],
            "broneering[broneeringuIsikud][0][synniaeg]": applicant["date_of_birth"],
            "broneering[broneeringuIsikud][0][telefon]": applicant["phone"],
            "broneering[broneeringuIsikud][0][epost]": applicant["email"],
            "broneering[broneeringuIsikud][0][viisataotluseNumber]": applicant["visa_application_number"],
            "broneering[_token]": token,
            "edasi": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, HTML_PARSER)
    cal = soup.find(attrs={"data-controller": "broneering"})
    if not cal:
        log("FAIL: calendar not reached")
        return 1
    token = extract_token(r.text) or token

    raw = cal.get("data-vastuvotukuupaevad")
    dates = json.loads(raw) if raw else []
    setup_elapsed = time.time() - _t0
    log(f"Session established in {setup_elapsed:.1f}s. Initial dates: {dates}")
    log(f"Polling every {POLL_INTERVAL_SECONDS}s for {DURATION_SECONDS}s. CSV: {csv_path}")
    log("From here on, every request is CAPTCHA-free and costs nothing.")

    fieldsets: list[str] = []
    poll_n = 0
    consecutive_failures = 0
    death_reason = None

    with open(csv_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "utc", "elapsed_s", "poll", "date", "http_status",
            "session_alive", "slot_count", "slot_times", "slot_ids", "query_ms",
        ])

        deadline = _t0 + DURATION_SECONDS
        while time.time() < deadline:
            poll_n += 1
            for iso in dates:
                d = datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
                q0 = time.time()
                try:
                    resp = s.post(
                        URL,
                        headers=turbo_headers,
                        data={
                            "broneering[kuupaev]": d,
                            "broneering[markused]": "",
                            "g-recaptcha-response": "",
                            "broneering[__dynamic_error]": "",
                            "broneering[_token]": token,
                            "turbo": "",
                        },
                        timeout=30,
                    )
                    status = resp.status_code
                    body = resp.text
                except Exception as e:
                    log(f"poll {poll_n} {iso}: request error {e}")
                    consecutive_failures += 1
                    writer.writerow([
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        f"{time.time() - _t0:.1f}", poll_n, iso, "ERR", "unknown", "", "", "",
                        f"{(time.time() - q0) * 1000:.0f}",
                    ])
                    fh.flush()
                    continue

                query_ms = (time.time() - q0) * 1000
                sp = BeautifulSoup(body, HTML_PARSER)
                boxes = sp.find_all("input", {"name": "broneering[vastuvotud][]"})
                times, ids = [], []
                for cb in boxes:
                    lab = sp.find("label", {"for": cb.get("id")}) if cb.get("id") else None
                    times.append(lab.get_text(strip=True) if lab else "?")
                    ids.append(cb.get("value"))

                # A dead session is the thing being measured, so it needs a
                # positive signal rather than being inferred from "no slots".
                # A live session always echoes back a fresh CSRF token; an
                # expired one bounces to the start of the form, where the
                # date field does not exist. Distinguishing "session died"
                # from "date genuinely has no free times" is the entire point
                # of the test, and conflating them would make the result
                # meaningless.
                new_token = extract_token(body)
                has_date_field = bool(sp.find("input", {"name": "broneering[kuupaev]"})) or bool(boxes)
                alive = bool(new_token) and has_date_field
                if new_token:
                    token = new_token

                writer.writerow([
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    f"{time.time() - _t0:.1f}", poll_n, iso, status,
                    alive, len(boxes), "|".join(times), "|".join(ids), f"{query_ms:.0f}",
                ])
                fh.flush()

                signature = f"{iso}:{'|'.join(times)}"
                if signature not in fieldsets:
                    fieldsets.append(signature)
                    log(f"poll {poll_n} {iso}: CHANGE -> {len(boxes)} slots {times} ({query_ms:.0f}ms)")
                elif poll_n % 10 == 1:
                    log(f"poll {poll_n} {iso}: {len(boxes)} slots {times} ({query_ms:.0f}ms, alive={alive})")

                if not alive:
                    consecutive_failures += 1
                    log(f"poll {poll_n} {iso}: SESSION LOOKS DEAD "
                        f"(token={'yes' if new_token else 'no'}, date_field={has_date_field})")
                else:
                    consecutive_failures = 0

            # Two in a row rather than one: a single odd response mid-poll is
            # not worth ending a multi-hour measurement over.
            if consecutive_failures >= 2:
                death_reason = f"session died after {time.time() - _t0:.0f}s ({poll_n} polls)"
                log(death_reason)
                break

            time.sleep(POLL_INTERVAL_SECONDS)

    total = time.time() - _t0
    log("=" * 70)
    log(f"RESULT: session survived {total - setup_elapsed:.0f}s of polling "
        f"({poll_n} polls) — {death_reason or 'still alive when the test ended'}")
    log(f"Distinct slot states seen: {len(fieldsets)}")
    for fs in fieldsets:
        log(f"    {fs}")
    log(f"CSV: {csv_path}")
    log("No booking was created; this script contains no submit path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
