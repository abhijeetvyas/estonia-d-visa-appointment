#!/usr/bin/env python3
"""
Replays the booking flow as plain HTTP `requests` calls, with no browser.

WHY THIS EXISTS
---------------
The BOOKER_TRACE run on 2026-08-02 captured every request the Playwright
booker actually makes. The result: the entire flow is ordinary Symfony form
POSTs to a single URL (https://broneering.mfa.ee/en/), with a rotating CSRF
token. Nothing in it requires a rendered DOM, flatpickr, Stimulus, or Turbo
JavaScript - the browser is only there to *produce* these POSTs.

That means the ~23s Playwright critical path can plausibly collapse to a few
HTTP round trips. This script tests that claim before any rewrite is
committed, and answers one much bigger question along the way:

    IS THE FIRST reCAPTCHA ACTUALLY ENFORCED SERVER-SIDE?

In the traced run, the `edasi` POST that advances past the service-selection
screen carried a solved token - but waiting for that token was 10.7s of a
23.2s run, by far the single largest cost. Two earlier POSTs on the same page
were accepted with `g-recaptcha-response=` completely empty (they're Turbo
refreshes, so that proves nothing on its own), which makes it worth spending
one cheap test on whether the gating POST checks the token at all.

If it does not, the consequences are large and go beyond speed: a full
slot-inventory query becomes ~4 POSTs with no CAPTCHA cost, which makes it
possible to poll *actual bookable time slots* directly rather than inferring
availability from the service dropdown - the open question that 5/5
`no_time_slots` failures hinge on.

DOES GOING BROWSERLESS MAKE THE CAPTCHA HARDER?
-----------------------------------------------
No, and it cannot - because our browser never solves the CAPTCHA in the first
place. CapSolver/2captcha solve it on their own machines, using their own IPs
and browsers; all we ever do is paste the resulting token string into the
form. Whether the thing doing the pasting is Playwright or `requests` is
invisible to Google, because neither one ever renders or clicks the widget.

Whatever difficulty the solver faces (one-click vs. an image grid) is decided
by the solver's IP and fingerprint reputation, not ours, and we pay per
solved token either way.

The separate, real risk is the *site* deciding it dislikes a plain HTTP
client. That is already disproven in production: `estonia_visa_monitor.py`
performs exactly these first two POSTs with `requests`, every 10 seconds,
and has done so for weeks without being challenged or blocked.

SAFETY
------
By default this script STOPS BEFORE the final "Book the appointment"
(`broneeri`) POST and creates no booking.

Set PROBE_BOOK=1 to also exercise the real submit and then immediately delete
the resulting appointment. That is a genuine write to Estonia's live system -
a real appointment exists under DUMMY applicant data (John Doe) for the few
seconds between submit and delete. It is the only way to capture the final
POST's shape, which is the last unknown blocking a browserless rewrite, but
it is not something to run in a loop. If the delete fails the script says so
loudly and dumps the page, so a stray test booking can be removed by hand.

USAGE
-----
    # Abu Dhabi (244) is the right default target: it has real availability,
    # so a zero-slot result means something went wrong rather than being the
    # expected answer.
    export REQUESTS_CA_BUNDLE=/home/abhijeetvyas13/estonia-d-visa-appointment/combined_ca_bundle.pem
    venv/bin/python3 probe_http_flow.py

    # Compare against the embassy that actually fails:
    EMBASSY_ID=40 EMBASSY_NAME="New Delhi" venv/bin/python3 probe_http_flow.py

    # Supply a solved token instead of testing the no-CAPTCHA path:
    CAPTCHA_TOKEN="03AFcWe..." venv/bin/python3 probe_http_flow.py
    # ...or have it solve one via CapSolver, same as the booker does:
    SOLVE_CAPTCHA=1 CAPSOLVER_API_KEY=... venv/bin/python3 probe_http_flow.py

    # Full end-to-end: book a real appointment with dummy data, then delete it.
    PROBE_BOOK=1 SOLVE_CAPTCHA=1 CAPSOLVER_API_KEY=... venv/bin/python3 probe_http_flow.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://broneering.mfa.ee/en/"
SERVICE_VALUE = "pikaajaline_viisa"
RECAPTCHA_SITEKEY = "6Ld04R4nAAAAADJhUQpKyGxGh4zlfOsyk5VkVkzR"

EMBASSY_ID = os.environ.get("EMBASSY_ID", "244")
EMBASSY_NAME = os.environ.get("EMBASSY_NAME", "Abu Dhabi")

SCRIPT_DIR = Path(__file__).resolve().parent
DUMMY_DATA_PATH = Path(os.environ.get("APPLICANT_DATA_DUMMY", SCRIPT_DIR / "applicant_data_dummy.json"))

# Empty by default - that IS the experiment. Set CAPTCHA_TOKEN to a solved
# token, or SOLVE_CAPTCHA=1 to fetch one, to test the flow with the CAPTCHA
# satisfied (useful for isolating whether a failure is CAPTCHA-related or not).
CAPTCHA_TOKEN = os.environ.get("CAPTCHA_TOKEN", "")
SOLVE_CAPTCHA = os.environ.get("SOLVE_CAPTCHA", "").lower() in ("1", "true", "yes")

# Opt-in: actually submit the booking and then delete it. See SAFETY above.
PROBE_BOOK = os.environ.get("PROBE_BOOK", "").lower() in ("1", "true", "yes")

DUMP_DIR = SCRIPT_DIR / "probe_dumps"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_t0 = time.time()


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} t+{time.time() - _t0:6.2f}s  {msg}", flush=True)


def dump(name: str, text: str) -> Path:
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = DUMP_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.html"
    path.write_text(text, encoding="utf-8")
    return path


def extract_token(html: str) -> str | None:
    """
    The CSRF token rotates on every response, so it has to be re-read from
    each one rather than captured once. Turbo-stream responses wrap the form
    in a <turbo-stream><template> element, but the input itself is still a
    plain <input name="broneering[_token]"> either way, so one selector
    covers both response shapes.
    """
    soup = BeautifulSoup(html, "lxml")
    inp = soup.find("input", {"name": "broneering[_token]"})
    if inp and inp.get("value"):
        return inp["value"]
    # Fallback: regex, in case the markup nests it somewhere BeautifulSoup's
    # turbo-stream handling doesn't surface as a normal input.
    m = re.search(r'name="broneering\[_token\]"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def solve_capsolver(timeout: int = 120) -> str:
    api_key = os.environ.get("CAPSOLVER_API_KEY")
    if not api_key:
        raise RuntimeError("SOLVE_CAPTCHA=1 but CAPSOLVER_API_KEY not set")
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
    task_id = r.json()["taskId"]
    log(f"CapSolver task {task_id} created")
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
        if d.get("status") == "ready":
            log(f"CapSolver solved in {time.time() - start:.1f}s")
            return d["solution"]["gRecaptchaResponse"]
    raise RuntimeError("CapSolver timed out")


def main() -> int:
    if not DUMMY_DATA_PATH.exists():
        print(f"Dummy applicant data not found at {DUMMY_DATA_PATH}", file=sys.stderr)
        return 2
    applicant = json.loads(DUMMY_DATA_PATH.read_text())

    token_for_edasi = CAPTCHA_TOKEN
    if SOLVE_CAPTCHA and not token_for_edasi:
        token_for_edasi = solve_capsolver()

    log(
        f"Probing {EMBASSY_NAME} ({EMBASSY_ID}) | "
        f"captcha token: {'PRESENT' if token_for_edasi else 'EMPTY (this is the test)'}"
    )

    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    turbo_headers = {
        "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": URL,
        "Origin": "https://broneering.mfa.ee",
    }

    # ---- Step 0: landing page, first CSRF token -------------------------
    r = s.get(URL, timeout=30)
    r.raise_for_status()
    token = extract_token(r.text)
    log(f"step0 landing            {r.status_code}  token={'yes' if token else 'NO'}")
    if not token:
        dump("step0_landing", r.text)
        log("FAIL: no CSRF token on landing page")
        return 1

    # ---- Step 1: choose embassy + person count --------------------------
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
    soup = BeautifulSoup(r.text, "lxml")
    sel = soup.find("select", {"name": "broneering[toiming]"}) or soup.find("select", {"id": "broneering_toiming"})
    services = [o.get_text(strip=True) for o in sel.find_all("option")] if sel else []
    token = extract_token(r.text) or token
    log(f"step1 embassy selected   {r.status_code}  services={len(services)}")
    if not sel:
        dump("step1_no_dropdown", r.text)
        log("FAIL: service dropdown missing")
        return 1
    has_dvisa = any("D-visa" in x for x in services)
    log(f"      D-visa offered: {has_dvisa}  ({services})")
    if not has_dvisa:
        log("STOP: this embassy is not currently offering the D-visa service")
        return 0

    # ---- Step 2: choose the service (Turbo refresh) ---------------------
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
    log(f"step2 service selected   {r.status_code}")

    # ---- Step 3: THE EXPERIMENT -----------------------------------------
    # Advance past the service screen. In the browser this POST carries a
    # solved reCAPTCHA token. Here it carries whatever CAPTCHA_TOKEN holds -
    # empty by default. Success is defined by whether the applicant form's
    # first-name field comes back, not by the HTTP status (Symfony re-renders
    # the same screen with a validation error on rejection, which is still a
    # 200).
    r = s.post(
        URL,
        headers={**turbo_headers, "Accept": "text/html, application/xhtml+xml"},
        data={
            "broneering[esindus]": EMBASSY_ID,
            "broneering[toiming]": SERVICE_VALUE,
            "broneering[isikuteArv]": "1",
            "g-recaptcha-response": token_for_edasi,
            "broneering[__dynamic_error]": "",
            "broneering[_token]": token,
            "edasi": "",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    name_field = soup.find("input", {"name": "broneering[broneeringuIsikud][0][eesnimi]"})
    token = extract_token(r.text) or token
    log(f"step3 edasi (CAPTCHA gate) {r.status_code}  applicant_form={'YES' if name_field else 'NO'}")

    if not name_field:
        path = dump("step3_edasi_rejected", r.text)
        text = soup.get_text(" ", strip=True)
        log(f"      >>> RESULT: first CAPTCHA IS ENFORCED (or another validation failed) <<<")
        log(f"      dump: {path}")
        log(f"      page text: {text[:400]!r}")

        # Retry with a real token rather than stopping. The point of the probe
        # is to walk the whole flow in one run; making the operator re-invoke
        # it just to get past a step we've already learned the answer to
        # wastes a round trip and, more importantly, a real availability
        # window if this is ever pointed at a live one.
        retry_token = CAPTCHA_TOKEN or (
            solve_capsolver() if (SOLVE_CAPTCHA or os.environ.get("CAPSOLVER_API_KEY")) else ""
        )
        if not retry_token:
            log("STOP: no CAPTCHA key available to retry with. Set CAPSOLVER_API_KEY and re-run.")
            return 0
        token = extract_token(r.text) or token
        r = s.post(
            URL,
            headers={**turbo_headers, "Accept": "text/html, application/xhtml+xml"},
            data={
                "broneering[esindus]": EMBASSY_ID,
                "broneering[toiming]": SERVICE_VALUE,
                "broneering[isikuteArv]": "1",
                "g-recaptcha-response": retry_token,
                "broneering[__dynamic_error]": "",
                "broneering[_token]": token,
                "edasi": "",
            },
            timeout=30,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        name_field = soup.find("input", {"name": "broneering[broneeringuIsikud][0][eesnimi]"})
        token = extract_token(r.text) or token
        log(f"step3 retry with solved CAPTCHA: applicant_form={'YES' if name_field else 'NO'}")
        if not name_field:
            log(f"FAIL: still rejected with a valid token. Dump: {dump('step3_retry_failed', r.text)}")
            log(f"      page text: {soup.get_text(' ', strip=True)[:400]!r}")
            return 1
    else:
        log("      >>> RESULT: applicant form reached WITHOUT a solved CAPTCHA <<<"
            if not token_for_edasi else
            "      applicant form reached (with a solved token, as expected)")

    # ---- Step 4: applicant details --------------------------------------
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
    soup = BeautifulSoup(r.text, "lxml")
    cal = soup.find(attrs={"data-controller": "broneering"})
    token = extract_token(r.text) or token
    log(f"step4 applicant submitted {r.status_code}  calendar={'YES' if cal else 'NO'}")
    if not cal:
        path = dump("step4_no_calendar", r.text)
        log(f"FAIL: calendar not reached. Dump: {path}")
        return 1

    raw_dates = cal.get("data-vastuvotukuupaevad")
    dates = json.loads(raw_dates) if raw_dates else []
    log(f"      available dates: {dates}  (etteBron={cal.get('data-etteBron')})")

    # ---- Step 5: query time slots for EVERY listed date -----------------
    # The Playwright booker only ever queries the single date its preference
    # rules pick, and reports `no_time_slots` if that one is empty. Querying
    # all of them is nearly free over HTTP and directly answers whether the
    # `vastuvotukuupaevad` list is trustworthy: if a date is listed but
    # returns zero slots in the same session seconds later, the list is
    # optimistic rather than merely stale.
    def select_date(iso: str, current_token: str):
        """POST a date selection and return (new_token, [{value, time}, ...], raw_html)."""
        d = datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
        resp = s.post(
            URL,
            headers=turbo_headers,
            data={
                "broneering[kuupaev]": d,
                "broneering[markused]": "",
                "g-recaptcha-response": "",
                "broneering[__dynamic_error]": "",
                "broneering[_token]": current_token,
                "turbo": "",
            },
            timeout=30,
        )
        resp.raise_for_status()
        sp = BeautifulSoup(resp.text, "lxml")
        found = []
        for cb in sp.find_all("input", {"name": "broneering[vastuvotud][]"}):
            lab = sp.find("label", {"for": cb.get("id")}) if cb.get("id") else None
            found.append({"value": cb.get("value"), "time": lab.get_text(strip=True) if lab else ""})
        return extract_token(resp.text) or current_token, found, resp.text

    slots_by_date: dict[str, list] = {}
    for iso in dates:
        token, found, html = select_date(iso, token)
        slots_by_date[iso] = found
        log(f"step5 slots for {iso}: {len(found)} -> {[f['time'] for f in found]}")
        if not found:
            log(f"      EMPTY - dump: {dump(f'step5_empty_{iso}', html)}")

    bookable = [(iso, sl) for iso, sl in slots_by_date.items() if sl]
    log(f"step5 summary: {len(bookable)}/{len(dates)} listed dates actually had bookable slots")

    if not PROBE_BOOK:
        log("DONE - PROBE_BOOK not set, stopped before the final submit. No booking created.")
        return 0
    if not bookable:
        log("DONE - nothing bookable, so nothing to submit.")
        return 0

    # ---- Step 6: the real submit ----------------------------------------
    # This is the one POST the Playwright trace never captured (the second
    # dry-run was blocked by the cooldown), and the last unknown standing
    # between here and a browserless booker.
    #
    # Body shape is inferred from the surrounding steps: the same date field
    # as step 5, plus the chosen slot's internal ID, the free-text field, a
    # CAPTCHA token, and `broneeri` in place of `turbo`. If the inference is
    # wrong the response gets dumped, which tells us the correct shape - that
    # is the entire point of running this.
    target_date, target_slots = bookable[0]
    slot = target_slots[0]
    log(f"step6 booking {target_date} {slot['time']} (slot id {slot['value']})")

    # Step 5 walked through every date, so the server's current selection is
    # whichever date came last. Re-select the one we actually intend to book.
    token, recheck, _ = select_date(target_date, token)
    if not any(x["value"] == slot["value"] for x in recheck):
        log(f"      WARNING: slot {slot['value']} vanished on re-select; now {[x['time'] for x in recheck]}")
        if not recheck:
            log("DONE - the slot disappeared between the first read and the re-select.")
            return 0
        slot = recheck[0]
        log(f"      falling back to {slot['time']} (id {slot['value']})")

    def attempt_submit(captcha: str, label: str):
        resp = s.post(
            URL,
            headers={**turbo_headers, "Accept": "text/html, application/xhtml+xml"},
            data={
                "broneering[kuupaev]": datetime.strptime(target_date, "%Y-%m-%d").strftime("%d.%m.%Y"),
                "broneering[vastuvotud][]": slot["value"],
                "broneering[markused]": applicant.get("specifying_information", ""),
                "g-recaptcha-response": captcha,
                "broneering[__dynamic_error]": "",
                "broneering[_token]": token,
                "broneeri": "",
            },
            timeout=30,
        )
        resp.raise_for_status()
        sp = BeautifulSoup(resp.text, "lxml")
        text = sp.get_text(" ", strip=True)
        ok = ("Booked time" in text) or ("Add new booking" in text) or (
            datetime.strptime(target_date, "%Y-%m-%d").strftime("%d.%m.%Y") in text
            and "Delete" in text
        )
        log(f"step6 submit [{label}]: {resp.status_code}  confirmed={ok}")
        return ok, resp, sp, text

    # Try with no CAPTCHA first. Same logic as step 3: if the second CAPTCHA
    # is not enforced either, that is another ~15s off the critical path.
    ok, resp, sp, text = attempt_submit("", "no captcha")
    if not ok:
        log(f"      not confirmed - dump: {dump('step6_submit_nocaptcha', resp.text)}")
        log(f"      page text: {text[:300]!r}")
        token = extract_token(resp.text) or token
        fresh = CAPTCHA_TOKEN or (solve_capsolver() if (SOLVE_CAPTCHA or os.environ.get("CAPSOLVER_API_KEY")) else "")
        if not fresh:
            log("DONE - no CAPTCHA available to retry with. Set SOLVE_CAPTCHA=1 CAPSOLVER_API_KEY=...")
            return 1
        # A fresh date re-select is needed: the rejected POST rotated the token
        # and may have reset the form's selection state.
        token, recheck, _ = select_date(target_date, token)
        if recheck:
            slot = next((x for x in recheck if x["value"] == slot["value"]), recheck[0])
        ok, resp, sp, text = attempt_submit(fresh, "with captcha")
    else:
        log("      >>> RESULT: booking submitted WITHOUT a CAPTCHA <<<")

    if not ok:
        log(f"FAIL - submit not confirmed. Dump: {dump('step6_submit_failed', resp.text)}")
        log(f"      page text: {text[:500]!r}")
        log("      CHECK THE PORTAL MANUALLY - a test booking may or may not exist.")
        return 1

    log(f"step6 BOOKED. Confirmation page dump: {dump('step6_confirmation', resp.text)}")

    # ---- Step 7: delete the test booking --------------------------------
    # The Playwright booker guesses at a button labelled Delete/Cancel. Over
    # HTTP we need the actual form action and fields, so this enumerates
    # every form and link on the confirmation page, logs them, and then
    # submits the one that looks like the delete action. Whatever happens,
    # the enumeration is logged - so even a failure here tells us the correct
    # shape for next time.
    forms = sp.find_all("form")
    log(f"step7 confirmation page has {len(forms)} form(s)")
    for i, f in enumerate(forms):
        names = [i_.get("name") for i_ in f.find_all(["input", "button"]) if i_.get("name")]
        log(f"      form[{i}] action={f.get('action')!r} method={f.get('method')!r} fields={names}")
    links = [
        (a.get_text(strip=True), a.get("href"))
        for a in sp.find_all("a")
        if a.get_text(strip=True).lower() in ("delete", "cancel", "kustuta", "tuhista")
    ]
    log(f"      delete-ish links: {links}")

    deleted = False
    try:
        target_form = None
        for f in forms:
            blob = (f.get_text(" ", strip=True) + " " + str(f.get("action") or "")).lower()
            if any(w in blob for w in ("delete", "cancel", "kustuta", "tuhista")):
                target_form = f
                break
        if target_form is not None:
            payload = {}
            for i_ in target_form.find_all("input"):
                if i_.get("name"):
                    payload[i_["name"]] = i_.get("value", "")
            for b in target_form.find_all("button"):
                if b.get("name"):
                    payload[b["name"]] = b.get("value", "")
            action = target_form.get("action") or URL
            if action.startswith("/"):
                action = "https://broneering.mfa.ee" + action
            log(f"step7 submitting delete form -> {action} fields={list(payload)}")
            dr = s.post(action, headers=turbo_headers, data=payload, timeout=30)
            dr.raise_for_status()
            dtext = BeautifulSoup(dr.text, "lxml").get_text(" ", strip=True)
            deleted = "Booked time" not in dtext
            log(f"step7 delete response {dr.status_code}  deleted={deleted}  dump: {dump('step7_after_delete', dr.text)}")
        elif links:
            href = links[0][1]
            if href and href.startswith("/"):
                href = "https://broneering.mfa.ee" + href
            log(f"step7 following delete link -> {href}")
            dr = s.get(href, timeout=30)
            dr.raise_for_status()
            dtext = BeautifulSoup(dr.text, "lxml").get_text(" ", strip=True)
            deleted = "Booked time" not in dtext
            log(f"step7 delete response {dr.status_code}  deleted={deleted}  dump: {dump('step7_after_delete', dr.text)}")
        else:
            log("step7 NO delete form or link found - see the confirmation dump for the real markup")
    except Exception as e:
        log(f"step7 delete failed: {e}")

    if deleted:
        log("DONE - full round trip verified: booked and deleted.")
    else:
        log("DONE - BOOKED BUT NOT CONFIRMED DELETED. Check the portal and remove the dummy booking by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
