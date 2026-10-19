"""Real mobile browser checks over disposable data; no real sends or machines."""
import json
import threading
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from prompt_notes import runner


@pytest.mark.browser
def test_mobile_drafts_and_warm_model_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            calls = []

            def models(route):
                provider = urlparse(route.request.url).path.rsplit("/", 1)[1]
                calls.append(provider)
                route.fulfill(json={"provider": provider, "available": True, "models": [
                    {"id": f"{provider}-one", "label": "First", "efforts": ["low", "high"]},
                    {"id": f"{provider}-two", "label": "Second", "efforts": ["low", "high"]},
                ]})

            page.route("**/api/chat-models/*", models)
            page.route("**/api/machines", lambda route: route.fulfill(json={
                "machines": [{"id": "test", "name": "Test machine"}], "current_machine_id": "test", "unavailable": False,
            }))
            page.route("**/api/fire", lambda route: route.abort())
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.wait_for_function("JSON.parse(localStorage.getItem('prompt-notes.models.v2') || '{}')[':codex'] !== undefined")
            page.wait_for_function("Object.keys(JSON.parse(localStorage.getItem('prompt-notes.models.v2') || '{}')).length === 4")
            assert set(calls) == {"claude", "codex", "agy", "pi-dev"}
            for button in page.locator("button:visible").all():
                box = button.bounding_box()
                assert box is not None
                assert box["width"] >= 44 and box["height"] >= 44
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_label("Draft title", exact=True).fill("Stale draft")
            page.get_by_label("Draft text", exact=True).fill("Remove only these words")
            page.wait_for_function("document.querySelector('#save-indicator').getAttribute('aria-label') === 'Saved'")
            page.locator("#drafts-button").click()
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.get_by_role("button", name="Delete Stale draft", exact=True).click()
            expect(page.get_by_role("button", name="Stale draft", exact=True)).to_be_visible()
            assert len(page.request.get(f"http://127.0.0.1:{server.server_port}/api/drafts").json()["drafts"]) == 1
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Delete Stale draft", exact=True).click()
            expect(page.get_by_role("button", name="Stale draft", exact=True)).to_have_count(0)
            page.locator("#list-fired").click()
            expect(page.get_by_role("img", name="Outbox empty")).to_be_visible()
            assert page.locator(".empty-outbox").inner_text() == ""
            page.get_by_role("button", name="New draft", exact=True).click()
            page.get_by_role("button", name="Continue", exact=True).click()
            page.wait_for_function("Object.keys(JSON.parse(localStorage.getItem('prompt-notes.models.v2') || '{}')).filter(key => key.startsWith('test:')).length === 4")
            for provider in ["codex", "agy", "pi-dev", "claude"]:
                page.locator("#provider").select_option(provider)
                expect(page.locator("#model")).to_have_value(f"{provider}-one")
                expect(page.locator("#model")).to_be_enabled()
            assert len(calls) == 8
            page.locator("#provider").select_option("codex")
            page.locator("#model").select_option("codex-two")
            page.locator("#effort").select_option("high")
            page.wait_for_function("JSON.parse(localStorage.getItem('prompt-notes.defaults.v1')).by_provider.codex.model === 'codex-two'")
            page.unroute("**/api/chat-models/*")
            page.route("**/api/chat-models/*", lambda route: route.abort())
            # A reload with the model service unavailable still uses cached choices.
            page.reload()
            page.get_by_role("button", name="Continue", exact=True).click()
            expect(page.locator("#provider")).to_have_value("codex")
            expect(page.locator("#model")).to_have_value("codex-two")
            expect(page.locator("#effort")).to_have_value("high")
            page.screenshot(path=str(tmp_path / "mobile-destination.png"))
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.browser
def test_follow_up_picks_a_machine_then_one_of_its_chats(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            chat_requests, fired = [], []
            chats = {"talker": [{"id": "t-1", "name": "Talker chat"}],
                     "worker": [{"id": "w-1", "name": "Fix the build"}, {"id": "w-2", "name": "Write notes"}]}

            def chat_names(route):
                machine = route.request.url.split("machine=", 1)[1]
                chat_requests.append(machine)
                route.fulfill(json={"chats": chats[machine]})

            def fire(route):
                fired.append(json.loads(route.request.post_data))
                route.fulfill(status=202, json={"id": "a-1", "title": "Ask", "body": "Hi", "state": "sending", "chat_id": "",
                                                "error": "", "destination": fired[-1]["destination"], "created_at": 1, "updated_at": 1})

            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
            page.route("**/api/machines", lambda route: route.fulfill(json={
                "machines": [{"id": "talker", "name": "talker-086"}, {"id": "worker", "name": "worker-086"}],
                "current_machine_id": "talker", "unavailable": False,
            }))
            page.route("**/api/chat-names?*", chat_names)
            page.route("**/api/fire", fire)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_label("Draft title", exact=True).fill("Ask")
            page.get_by_label("Draft text", exact=True).fill("Hi")
            page.get_by_role("button", name="Continue", exact=True).click()
            page.locator("#follow-up").click()
            expect(page.locator("#machine")).to_be_visible()
            expect(page.locator("#machine")).to_have_value("talker")
            expect(page.locator("#target-chat option")).to_have_text(["Talker chat"])
            page.locator("#machine").select_option("worker")
            expect(page.locator("#target-chat option")).to_have_text(["Fix the build", "Write notes"])
            page.locator("#target-chat").select_option("w-2")
            page.locator("#machine").select_option("talker")
            page.locator("#machine").select_option("worker")
            assert chat_requests.count("worker") == 1   # each machine's chats are fetched once
            page.locator("#target-chat").select_option("w-2")
            page.locator("#preview-button").click()
            page.wait_for_function("document.getElementById('editor-view').hidden === false")
            assert fired[0]["destination"] == {"mode": "follow-up", "chat_id": "w-2", "machine": "worker",
                                               "machine_name": "worker-086", "chat_name": "Write notes"}
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
