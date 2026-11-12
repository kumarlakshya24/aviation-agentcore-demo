"""Capture README screenshots of the offline demo with headless Chromium.

Starts app.py in offline mode on 127.0.0.1, drives the UI with Playwright,
writes docs/assets/*.png, then stops the server.

    uv run --with playwright python scripts/capture_screenshots.py
    (first time: uv run --with playwright playwright install chromium)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
PORT = os.environ.get("CAPTURE_PORT", "8765")
URL = f"http://127.0.0.1:{PORT}"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("capture")


def wait_for_server(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/api/health", timeout=1):  # noqa: S310 (fixed loopback URL)
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"server did not start on {URL}")


def ask(page: Page, question: str) -> None:
    page.fill("#chatInput", question)
    page.click("#chatSend")
    page.wait_for_function("!document.getElementById('chatInput').disabled", timeout=10_000)
    page.wait_for_timeout(400)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DEMO_MODE": "offline", "HOST": "127.0.0.1", "PORT": PORT, "AWS_REGION": "us-east-1"}
    server = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env)  # noqa: S603
    try:
        wait_for_server()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page.goto(URL)
            page.wait_for_function("document.getElementById('metricAlarms').textContent !== '--'")
            page.wait_for_timeout(1200)  # let the entry animations finish
            page.screenshot(path=ASSETS / "dashboard.png", full_page=True)
            log.info("wrote dashboard.png")

            page.click("#chatFab")
            page.wait_for_timeout(500)
            ask(page, "What CloudWatch alarms are currently firing?")
            page.locator(".tool-call-header").last.click()
            page.wait_for_timeout(300)
            page.screenshot(path=ASSETS / "chat-alarms.png")
            log.info("wrote chat-alarms.png")

            ask(page, "Scale the telemetry processor to 3 desired tasks")
            ask(page, "Yes, proceed")
            page.locator(".tool-call-header").last.click()
            page.wait_for_timeout(300)
            page.locator(".chat-bubble.assistant").last.scroll_into_view_if_needed()
            page.screenshot(path=ASSETS / "chat-scale-confirm.png")
            log.info("wrote chat-scale-confirm.png")

            page.click("#chatCloseBtn")
            page.click("#refreshTracesBtn")
            page.wait_for_timeout(600)
            page.locator(".traces-table").scroll_into_view_if_needed()
            page.screenshot(path=ASSETS / "traces.png")
            log.info("wrote traces.png")
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
