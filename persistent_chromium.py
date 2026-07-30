#!/usr/bin/env python3
"""
Keeps a single headless Chromium instance alive indefinitely with a local
Chrome DevTools Protocol (CDP) port open, so estonia_d_visa_booker.py can
attach to it via Playwright's connect_over_cdp() instead of cold-launching a
fresh browser on every triggered booking attempt.

Why this exists: on this VM (shared-core e2 family, GCP Always Free tier),
a fresh `chromium.launch()` was observed taking 10-30s+, and considerably
longer under CPU-credit throttling from back-to-back runs. That cost lands
exactly when speed matters most - immediately after a slot is detected, in a
race against a window that can close in under a minute. Playwright's
connect_over_cdp() attaches to an already-running browser in ~1-2s (just a
websocket handshake), so keeping one warm removes almost all of that cost.

This process is meant to run continuously as a systemd service (see
BOOKER_README.md for the unit file) so it survives reboots and auto-restarts
if Chromium itself ever crashes. estonia_d_visa_booker.py already falls back
to a fresh cold-launch if this isn't reachable, so it's safe for this service
to be down/not-yet-set-up - nothing else depends on it being up.

Usage:
    venv/bin/python3 persistent_chromium.py
    (or via the systemd service - see BOOKER_README.md)

Env vars:
    PERSISTENT_CHROMIUM_CDP_PORT   default: 9222
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

CDP_PORT = int(os.environ.get("PERSISTENT_CHROMIUM_CDP_PORT", "9222"))

_shutdown = False


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} {msg}", flush=True)


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _log(f"Received signal {signum} - shutting down")
    _shutdown = True


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1"],
        )
        _log(f"Persistent Chromium launched - CDP listening on 127.0.0.1:{CDP_PORT}")

        try:
            while not _shutdown:
                time.sleep(5)
                if not browser.is_connected():
                    _log("Chromium disconnected unexpectedly - exiting so systemd restarts us")
                    return 1
        finally:
            _log("Closing persistent Chromium")
            try:
                browser.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
