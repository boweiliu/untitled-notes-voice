"""Connected clients discover frontend changes without risking unsaved notes."""
import re
import shutil
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from prompt_notes import runner


def test_frontend_identity_tracks_content_and_is_never_cached(tmp_path, monkeypatch) -> None:
    assets = tmp_path / "assets"
    shutil.copytree(runner.FRONTEND_DIR, assets)
    monkeypatch.setattr(runner, "FRONTEND_DIR", assets)
    client = runner.app.test_client()
    response = client.get("/api/frontend-version")
    version = response.get_json()["version"]
    assert re.fullmatch(r"[a-f0-9]{64}", version)
    assert response.headers["Cache-Control"] == "no-store"
    page = client.get("/")
    assert page.headers["Cache-Control"] == "no-store"
    assert f"const loadedFrontendVersion = '{version}'" in page.text
    assert f'/voice/voice_capture.js?v={version}' in page.text
    assert client.get("/api/frontend-version").get_json()["version"] == version
    for name in ("app.html", "voice_capture.js", "new.css", "pwa/icon.svg"):
        path = assets / name
        path.write_bytes((path.read_bytes() if path.exists() else b"") + b"\n/* update */")
        changed = client.get("/api/frontend-version").get_json()["version"]
        assert changed != version, name
        version = changed
    # Changes to tests and user records do not tell clients to refresh.
    (assets / "ignored.test.mjs").write_text("changed")
    (tmp_path / "draft.txt").write_text("new note")
    assert client.get("/api/frontend-version").get_json()["version"] == version


@pytest.mark.browser
def test_every_client_gets_persistent_notice_and_refresh_preserves_text(tmp_path, monkeypatch) -> None:
    assets = tmp_path / "assets"
    shutil.copytree(runner.FRONTEND_DIR, assets)
    monkeypatch.setattr(runner, "FRONTEND_DIR", assets)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"]
            )
            desktop = browser.new_context(viewport={"width": 1100, "height": 800})
            mobile = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
            pages = [desktop.new_page(), mobile.new_page()]
            errors = []
            for page in pages:
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"available": True, "models": []}))
                page.route("https://**", lambda route: route.abort())
                page.goto(url)
                expect(page.locator("#update-notice")).to_be_hidden()
            path = assets / "voice_capture.js"
            path.write_text(path.read_text() + "\n// New frontend deployment\n")
            # Exercise the real periodic checks on two independent clients.
            for page in pages:
                expect(page.locator("#update-notice")).to_be_visible(timeout=15000)
            phone = pages[1]
            assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth")
            refresh_box = phone.locator("#refresh-app").bounding_box()
            assert refresh_box is not None and refresh_box["height"] >= 44
            phone.get_by_label("Draft title", exact=True).fill("Keep this title")
            phone.get_by_label("Draft text", exact=True).fill("Unsaved words survive refresh")
            phone.route("**/api/drafts", lambda route: route.abort() if route.request.method == "POST" else route.continue_())
            phone.locator("#refresh-app").click()
            expect(phone.locator("#update-message")).to_contain_text("Could not refresh safely")
            expect(phone.get_by_label("Draft text", exact=True)).to_have_value("Unsaved words survive refresh")
            phone.unroute("**/api/drafts")
            phone.locator("#refresh-app").click()
            expect(phone.locator("#update-notice")).to_be_hidden()
            expect(phone.get_by_label("Draft title", exact=True)).to_have_value("Keep this title")
            expect(phone.get_by_label("Draft text", exact=True)).to_have_value("Unsaved words survive refresh")
            expect(pages[0].locator("#update-notice")).to_be_visible()
            path.write_text(path.read_text() + "\n// Another deployment\n")
            phone.evaluate("window.dispatchEvent(new Event('pageshow'))")
            expect(phone.locator("#update-notice")).to_be_visible()
            # A restart/offline response never removes an already displayed notice.
            phone.route("**/api/frontend-version", lambda route: route.abort())
            phone.evaluate("window.dispatchEvent(new Event('online'))")
            expect(phone.locator("#update-notice")).to_be_visible()
            # The refresh guard must leave a live dictation session alone.
            phone.evaluate("voiceState = 'listening'")
            phone.locator("#refresh-app").click()
            expect(phone.locator("#update-message")).to_have_text("Stop dictation, then click to refresh.")
            assert phone.evaluate("voiceState") == "listening"
            phone.evaluate("voiceState = 'idle'")
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
