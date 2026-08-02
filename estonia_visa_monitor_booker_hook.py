#!/usr/bin/env python3
"""
Thin wrapper around the original estonia_visa_monitor.py that, when a slot is
detected, also launches the Playwright booker (once, under lock/cooldown).

This file is the only place that knows about the booker.  The original
estonia_visa_monitor.py stays completely untouched.

Usage (on the VM):
  - Keep the existing run_monitor.sh pointing at the original monitor, OR
  - Point it at this file instead when you want the booker integrated.

Environment (in addition to the original monitor vars):
  ENABLE_BOOKER=1          – master switch; without it this behaves exactly
                             like the original monitor
  BOOKER_SCRIPT            – path to estonia_d_visa_booker.py
                             (default: same directory)
  All booker env vars (TWOCAPTCHA_API_KEY, APPLICANT_DATA_*, DRY_RUN, etc.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Make sure the original monitor is importable from the same directory
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Import the original monitor's public entry points / helpers
import estonia_visa_monitor as mon  # noqa: E402

BOOKER_SCRIPT = Path(os.environ.get("BOOKER_SCRIPT", SCRIPT_DIR / "estonia_d_visa_booker.py"))
ENABLE_BOOKER = os.environ.get("ENABLE_BOOKER", "").lower() in ("1", "true", "yes")

# --- Parked-Session Booker Daemon trigger file (spec R5-R7) -----------------
# This is the ONLY hook the daemon (`estonia_booker_daemon.py`) has into
# detection. It is intentionally decoupled from ENABLE_BOOKER (the old
# Playwright-booker switch) and from the daemon actually running at all -
# the daemon polls this file every 0.5s (R7) and treats a newer timestamp as
# a fresh trigger; if the daemon isn't running, the file is just written and
# never read, with zero effect on anything else (R4).
STATE_DIR = SCRIPT_DIR / ".state"


def write_daemon_trigger(available_services: list) -> None:
    """R5: atomically write .state/trigger_<EMBASSY_ID>.json with
    {"ts": <unix time>, "services": [...]}. R6: any exception here must be
    caught and logged WITHOUT affecting the alerting path above this call -
    a bug in this brand-new, optional trigger mechanism must never be able
    to take down the one thing (Telegram/Twilio alerting) the owner
    explicitly requires to always work (spec R1-R4)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        trigger_path = STATE_DIR / f"trigger_{mon.EMBASSY_ID}.json"
        payload = {"ts": time.time(), "services": list(available_services)}
        # Atomic write: temp file in the same directory (so os.replace() is
        # a same-filesystem rename, not a cross-device copy) then os.replace,
        # so the daemon's 0.5s poll never sees a half-written file.
        fd, tmp_path = tempfile.mkstemp(dir=str(STATE_DIR), prefix=".trigger_tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp_path, trigger_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        mon.log_line(f"Daemon trigger file written: {trigger_path}")
    except Exception as e:
        # R6: never let this bubble up into the caller's alerting path.
        mon.log_line(f"Failed to write daemon trigger file (non-fatal, alerting unaffected): {e}", file=sys.stderr)


def trigger_booker() -> None:
    """Launch the booker as a separate process so a crash cannot take down the monitor."""
    if not BOOKER_SCRIPT.exists():
        mon.log_line(f"Booker script not found at {BOOKER_SCRIPT} – skipping", file=sys.stderr)
        return

    env = os.environ.copy()
    # Inherit embassy settings
    env.setdefault("EMBASSY_ID", mon.EMBASSY_ID)
    env.setdefault("EMBASSY_NAME", mon.EMBASSY_NAME)

    mon.log_line(f"Launching booker: {BOOKER_SCRIPT}")
    try:
        # Detach so the monitor returns quickly; the booker has its own lock.
        subprocess.Popen(
            [sys.executable, str(BOOKER_SCRIPT)],
            env=env,
            stdout=open(SCRIPT_DIR / "booker.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        mon.log_line("Booker process started (see booker.log)")
    except Exception as e:
        mon.log_line(f"Failed to launch booker: {e}", file=sys.stderr)
        try:
            mon.send_telegram_text(
                f"⚠️ Failed to launch D-visa booker for {mon.EMBASSY_NAME}: {e}\n\n"
                f"— Sent from {os.environ.get('RUNTIME_LABEL', 'GCP')}"
            )
        except Exception:
            pass


def main() -> None:
    if os.environ.get("TEST_TELEGRAM", "").lower() in ("1", "true", "yes"):
        mon.send_test_alert()
        return

    try:
        try:
            found, available = mon.check_availability()
        except Exception as e:
            mon.log_line(f"First attempt failed ({e}), retrying once...", file=sys.stderr)
            found, available = mon.check_availability()
    except Exception as exc:
        mon.handle_failure(exc)
        sys.exit(1)

    mon.clear_failure_state_if_needed()

    if found:
        # Written FIRST, before the Telegram/Twilio alert: the whole point of
        # the parked-session daemon (SPEC §1) is cutting detection-to-submit
        # latency to ~1s, and the daemon polls this file every 0.5s (R7) - a
        # slow or failing Telegram/Twilio call must not add its own latency
        # in front of that. write_daemon_trigger() is independently
        # exception-safe (R6), so this ordering cannot make alerting any less
        # reliable than before.
        write_daemon_trigger(available)

        mon.send_availability_alert(available)
        mon.log_line(f"AVAILABLE: {mon.TARGET}. Telegram sent. Services: {available}")

        if ENABLE_BOOKER:
            trigger_booker()
        else:
            mon.log_line("ENABLE_BOOKER not set – booker not launched")
    else:
        mon.log_line(f"NOT available. Services: {available}")


if __name__ == "__main__":
    main()
