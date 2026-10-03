"""Exercise the real local demo in a browser and save desktop/mobile review images.

Run with `.venv/Scripts/python scripts/ui_smoke.py` (Windows), or `python scripts/ui_smoke.py`.
The script starts an isolated test server and removes its temporary source database on exit.
It uses an installed Edge/Chrome browser, falling back to Playwright Chromium.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import Error, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
BASE_URL = "http://127.0.0.1:8765"


def launch_browser(playwright):
    errors = []
    for channel in ("msedge", "chrome", None):
        try:
            return playwright.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
        except Error as exc:
            errors.append(str(exc).splitlines()[0])
    raise RuntimeError("No browser available. Run 'playwright install chromium'. " + "; ".join(errors))


def check_no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Horizontal overflow"


def run_browser_checks():
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1,
                                      reduced_motion="reduce")
        page = context.new_page()
        script_errors = []
        page.on("pageerror", lambda error: script_errors.append(str(error)))
        try:
            page.goto(BASE_URL, wait_until="networkidle")
            expect(page.get_by_role("heading", name="Your team's knowledge.")).to_be_visible()
            expect(page.locator("#sidebar-status")).to_have_text("Workspace is connected")
            expect(page.locator("#document-count")).to_have_text("4")
            check_no_overflow(page)
            page.screenshot(path=str(ARTIFACTS / "ui-desktop.png"), full_page=True)

            page.locator("[data-question]").first.click()
            expect(page.locator("#question")).to_have_value("Why is the Payments release delayed, and where is its rollout guide?")
            page.locator("#send-button").click()
            expect(page.locator(".answer-body")).to_be_visible(timeout=60000)
            expect(page.locator(".answer-body")).to_contain_text("idempotency")
            expect(page.locator(".citation-card").first).to_be_visible()
            expect(page.locator("#stop-button")).to_be_hidden()
            page.screenshot(path=str(ARTIFACTS / "ui-answer.png"), full_page=True)

            page.locator(".citation-card").filter(has_text="Payments rollout guide").first.click()
            expect(page.locator("#citation-dialog")).to_be_visible()
            expect(page.locator(".citation-excerpt")).to_contain_text("5% canary", timeout=10000)
            page.locator("#close-citation").click()

            expect(page.locator(".session-button").first).to_be_visible()
            page.locator("#new-chat").click()
            expect(page.locator("#welcome")).to_be_visible()
            page.locator(".session-button").first.click()
            expect(page.locator(".answer-body")).to_contain_text("idempotency")

            page.locator('[data-view="knowledge"]').click()
            expect(page.locator(".document-row")).to_have_count(4)
            page.locator("#file-input").set_input_files({
                "name": "atlas-smoke-guide.md",
                "mimeType": "text/markdown",
                "buffer": b"# Atlas browser smoke guide\n\nThe Atlas smoke team owns the browser verification guide. "
                          b"Run desktop and mobile checks before accepting a workspace release. "
                          b"Inspect the source citations and confirm the uploaded document is searchable.\n",
            })
            expect(page.locator("#upload-status")).to_contain_text("indexed and ready", timeout=60000)
            expect(page.locator(".document-row")).to_have_count(5)
            page.locator("#document-search").fill("Atlas browser smoke")
            expect(page.locator(".document-row")).to_have_count(1)
            page.locator(".document-row .icon-button").click()
            page.locator("#accept-confirm").click()
            expect(page.locator("#table-document-count")).to_have_text("4")
            page.locator("#document-search").fill("")
            expect(page.locator(".document-row")).to_have_count(4)
            page.screenshot(path=str(ARTIFACTS / "ui-knowledge.png"), full_page=True)

            page.locator('[data-view="activity"]').click()
            expect(page.locator(".activity-row").first).to_be_visible()
            expect(page.locator("#stat-total")).to_have_text("1")
            expect(page.locator("#stat-success")).to_have_text("1")

            # Mobile shares the same real session but starts with the welcome view.
            mobile_context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=1,
                                                 is_mobile=True, has_touch=True, reduced_motion="reduce")
            mobile = mobile_context.new_page()
            mobile.on("pageerror", lambda error: script_errors.append(str(error)))
            mobile.goto(BASE_URL, wait_until="networkidle")
            expect(mobile.locator("#welcome")).to_be_visible()
            check_no_overflow(mobile)
            mobile.screenshot(path=str(ARTIFACTS / "ui-mobile.png"), full_page=True)
            mobile.locator("#mobile-menu").click()
            expect(mobile.locator("#sidebar")).to_have_class("sidebar open")
            mobile.locator('[data-view="knowledge"]').click()
            expect(mobile.locator("#view-knowledge")).to_be_visible()
            expect(mobile.locator(".document-row")).to_have_count(4)
            check_no_overflow(mobile)
            mobile.screenshot(path=str(ARTIFACTS / "ui-mobile-knowledge.png"), full_page=True)
            mobile_context.close()
            assert not script_errors, "Browser script errors: " + " | ".join(script_errors)
            print("PASS: desktop, mobile, real SSE answer, citations, authenticated source preview, history, upload/delete/search, activity")
            print(f"Screenshots: {ARTIFACTS}")
        except Exception:
            page.screenshot(path=str(ARTIFACTS / "ui-failure.png"), full_page=True)
            raise
        finally:
            browser.close()


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="atlas-ui-smoke-", ignore_cleanup_errors=True) as data_dir:
        env = {**os.environ, "DATA_DIR": data_dir, "APP_ENV": "test", "MODEL_MODE": "demo",
               "AUTH_MODE": "demo", "BOOTSTRAP_DEMO": "true", "MCP_TRANSPORT": "stdio"}
        with (ARTIFACTS / "ui-smoke-server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "src.api.main:app", "--host", "127.0.0.1", "--port", "8765"],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError("UI test server exited; inspect artifacts/ui-smoke-server.log")
                    try:
                        with urlopen(f"{BASE_URL}/health/live", timeout=1) as response:
                            if response.status == 200:
                                break
                    except (URLError, TimeoutError):
                        time.sleep(0.25)
                else:
                    raise RuntimeError("UI test server did not become ready")
                run_browser_checks()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                # Windows releases SQLite file handles shortly after process exit.
                time.sleep(0.5)


if __name__ == "__main__":
    main()
