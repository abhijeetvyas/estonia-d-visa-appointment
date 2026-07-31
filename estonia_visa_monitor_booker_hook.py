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

import os
import subprocess
import sys
from pathlib import Path

# Make sure the original monitor is importable from the same directory
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Import the original monitor's public entry points / helpers
import estonia_visa_monitor as mon  # noqa: E402

BOOKER_SCRIPT = Path(os.environ.get("BOOKER_SCRIPT", SCRIPT_DIR / "estonia_d_visa_booker.py"))
ENABLE_BOOKER = os.environ.get("ENABLE_BOOKER", "").lower() in ("1", "true", "yes")


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
