"""On a phone the bottom controls stay on screen: above the browser's toolbar and the keyboard."""
import threading

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from prompt_notes import runner

FORTRESS = "/opt/fortress/tilion-fortress/tilion"
LONG_TEXT = "\n".join(f"Line {number} of a long dictated draft." for number in range(120))


def fully_visible(page, selector):
    box = page.locator(selector).bounding_box()
    height = page.evaluate("window.innerHeight")
    return box is not None and box["y"] >= 0 and box["y"] + box["height"] <= height


@pytest.mark.browser
@pytest.mark.parametrize("height", [740, 360])   # a phone screen, and the same phone with the keyboard up
def test_bottom_controls_stay_on_screen(tmp_path, monkeypatch, height):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 360, "height": height}, is_mobile=True, has_touch=True)
            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
            page.goto(f"http://127.0.0.1:{server.port}/")
            assert "interactive-widget=resizes-content" in (page.locator('meta[name="viewport"]').get_attribute("content") or "")
            page.locator("#draft-text").fill(LONG_TEXT)
            for scroll in (0, 99999):
                page.evaluate(f"window.scrollTo(0, {scroll})")
                assert fully_visible(page, "#voice-button"), f"mic hidden at scroll {scroll}"
                assert fully_visible(page, "#continue-button"), f"continue hidden at scroll {scroll}"
            page.locator("#continue-button").click()
            assert fully_visible(page, "#preview-button")
            page.locator("#drafts-view .back-button").wait_for(state="hidden")
            browser.close()
    finally:
        server.shutdown()
