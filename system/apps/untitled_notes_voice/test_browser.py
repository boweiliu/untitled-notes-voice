"""Real mobile browser checks over disposable data; no real sends or machines."""
import json
import re
import threading
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from prompt_notes import runner


@pytest.mark.browser
def test_mobile_drafts_and_warm_model_choices(tmp_path, monkeypatch) -> None:
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
            page.wait_for_function("JSON.parse(localStorage.getItem('prompt-notes.models.v3') || '{}')[':codex'] !== undefined")
            page.wait_for_function("Object.keys(JSON.parse(localStorage.getItem('prompt-notes.models.v3') || '{}')).length === 4")
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
            page.wait_for_function("Object.keys(JSON.parse(localStorage.getItem('prompt-notes.models.v3') || '{}')).filter(key => key.startsWith('test:')).length === 4")
            for provider in ["codex", "agy", "pi-dev", "claude"]:
                page.locator("#provider").select_option(provider)
                expect(page.locator("#model")).to_have_value(f"{provider}-one")
                expect(page.locator("#model")).to_be_enabled()
            assert len(calls) == 8
            page.locator("#provider").select_option("codex")
            page.locator("#model").select_option("codex-two")
            page.locator("#effort").select_option("high")
            page.wait_for_function("JSON.parse(localStorage.getItem('prompt-notes.defaults.v2')).by_machine.test.by_provider.codex.model === 'codex-two'")
            # Reload after the queued defaults write has reached the durable store.
            page.wait_for_function("""async () => {
                const saved = await (await fetch('/api/destination-defaults')).json();
                const chosen = saved.by_machine?.test?.by_provider?.codex;
                return saved.by_machine?.test?.provider === 'codex' && chosen?.model === 'codex-two'
                    && chosen?.effort_by_model?.['codex-two'] === 'high';
            }""")
            page.wait_for_load_state("networkidle")
            page.evaluate("defaultsWrite")
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
def test_a_new_agy_chat_is_sent_with_the_chosen_model_and_its_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
            errors, fired = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def fire(route):
                fired.append(json.loads(route.request.post_data))
                route.fulfill(status=202, json={"id": "a-1", "title": "Ask", "body": "Hi", "state": "sending", "chat_id": "",
                                                "error": "", "destination": fired[-1]["destination"], "created_at": 1, "updated_at": 1})

            agy_models = [
                {"id": "provider-default", "label": "Agy's own default model", "efforts": []},
                {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "efforts": ["low", "medium", "high"]},
            ]
            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={
                "provider": "agy", "available": True, "models": agy_models if route.request.url.endswith("/agy?machine=test") else []}))
            page.route("**/api/machines", lambda route: route.fulfill(json={
                "machines": [{"id": "test", "name": "Test machine"}], "current_machine_id": "test", "unavailable": False}))
            page.route("**/api/fire", fire)
            page.route("**/api/fired", lambda route: route.fulfill(json={"actions": [
                {"id": "a-1", "title": "Ask", "body": "Hi", "state": "sending", "chat_id": "", "error": "",
                 "destination": fired[-1]["destination"], "created_at": 1, "updated_at": 1}
            ] if fired else []}))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_label("Draft title", exact=True).fill("Ask")
            page.get_by_label("Draft text", exact=True).fill("Hi")
            page.get_by_role("button", name="Continue", exact=True).click()
            page.locator("#provider").select_option("agy")
            expect(page.locator("#model option")).to_have_text(["Agy's own default model", "Claude Opus 5.5"])
            page.locator("#model").select_option("claude-opus-5-5")
            # Effort is its own choice, as for the other providers.
            expect(page.locator("#effort option")).to_have_text(["low", "medium", "high"])
            page.locator("#effort").select_option("high")
            page.locator("#preview-button").click()
            page.wait_for_function("document.getElementById('editor-view').hidden === false")
            destination = fired[0]["destination"]
            assert (destination["provider"], destination["model"], destination["model_label"], destination["effort"]) == (
                "agy", "claude-opus-5-5", "Claude Opus 5.5", "high")
            # The sent view names the model as the user saw it, not by its id.
            page.locator("#fired-button").click()
            expect(page.locator(".fired-row").first).to_have_attribute("data-tip-more", re.compile(r"\(agy Claude Opus 5\.5 high\)"))
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.browser
def test_follow_up_picks_a_machine_then_one_of_its_chats(tmp_path, monkeypatch) -> None:
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
                "machines": [{"id": "talker", "name": "primary-example"}, {"id": "worker", "name": "secondary-example"}],
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
                                               "machine_name": "secondary-example", "chat_name": "Write notes"}
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.browser
def test_tab_and_shift_tab_move_between_title_and_note_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
            page.route("**/api/machines", lambda route: route.fulfill(json={"machines": [], "current_machine_id": "", "unavailable": True}))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            title = page.get_by_label("Draft title", exact=True)
            title.click()
            page.keyboard.type("Groceries")
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "draft-text"
            page.keyboard.type("eggs")
            expect(page.get_by_label("Draft text", exact=True)).to_have_value("eggs")
            expect(title).to_have_value("Groceries")
            # Shift+Tab goes straight back to the title, nothing typed is lost, and Tab goes forward again.
            page.keyboard.press("Shift+Tab")
            assert page.evaluate("document.activeElement.id") == "draft-title"
            page.keyboard.type("!")
            expect(title).to_have_value("Groceries!")
            expect(page.get_by_label("Draft text", exact=True)).to_have_value("eggs")
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "draft-text"
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.browser
def test_escape_steps_back_out_of_screens_and_ctrl_enter_moves_on_to_destination(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            sent = {"id": "a-1", "title": "Sent note", "body": "Already gone", "state": "checking", "chat_id": "", "error": "",
                    "destination": {"mode": "new", "chat_name": "", "machine": "talker", "machine_name": "primary-example",
                                    "provider": "claude", "model": "m", "effort": ""},
                    "created_at": 1, "updated_at": 1}
            page.route("**/api/fired", lambda route: route.fulfill(json={"actions": [sent]}))
            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
            page.route("**/api/machines", lambda route: route.fulfill(json={"machines": [], "current_machine_id": "", "unavailable": True}))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            text = page.get_by_label("Draft text", exact=True)
            title = page.get_by_label("Draft title", exact=True)
            editor, drafts, setup = page.locator("#editor-view"), page.locator("#drafts-view"), page.locator("#setup-view")

            def on_screen(name):
                expect({"editor": editor, "drafts": drafts, "setup": setup}[name]).to_be_visible()
                for other in {"editor", "drafts", "setup"} - {name}:
                    expect({"editor": editor, "drafts": drafts, "setup": setup}[other]).to_be_hidden()

            # The hints on the buttons mention the shortcuts.
            expect(page.locator("#continue-button")).to_have_attribute("data-tip-more", re.compile("Shortcut: Ctrl\\+Enter\\."))
            expect(page.locator("#drafts-view .back-button")).to_have_attribute("data-tip-more", re.compile("Esc"))

            # Escape in the note itself does nothing; Ctrl+Enter from the text moves on to the destination screen.
            title.fill("Groceries")
            text.click()
            page.keyboard.type("eggs")
            page.keyboard.press("Escape")
            on_screen("editor")
            page.keyboard.press("Control+Enter")
            on_screen("setup")
            expect(text).to_have_value("eggs")

            # Escape leaves the destination screen for the note, with the cursor back in the text.
            page.keyboard.press("Escape")
            on_screen("editor")
            assert page.evaluate("document.activeElement.id") == "draft-text"
            expect(title).to_have_value("Groceries")
            expect(text).to_have_value("eggs")

            # Ctrl+Enter does the same from the title; the Mac key means nothing on this system.
            title.click()
            page.keyboard.press("Meta+Enter")
            on_screen("editor")
            page.keyboard.press("Control+Enter")
            on_screen("setup")
            page.keyboard.press("Escape")
            on_screen("editor")
            # Enter alone, Shift+Enter and Alt+Enter are not the shortcut.
            text.click()
            page.keyboard.press("Enter")
            page.keyboard.press("Shift+Enter")
            page.keyboard.press("Alt+Enter")
            on_screen("editor")
            expect(text).to_have_value("eggs\n\n")

            # Escape leaves the Drafts list, then the Fired list, for the note.
            page.get_by_role("button", name="Drafts", exact=True).first.click()
            on_screen("drafts")
            page.keyboard.press("Escape")
            on_screen("editor")
            expect(title).to_have_value("Groceries")
            page.get_by_role("button", name="Fired", exact=True).first.click()
            on_screen("drafts")
            page.keyboard.press("Escape")
            on_screen("editor")

            # A sent note goes back to the Fired list, then to the note; Ctrl+Enter does not send it on.
            page.get_by_role("button", name="Fired", exact=True).first.click()
            page.get_by_role("button", name="Sent note, checking").click()
            expect(page.get_by_label("Draft title", exact=True)).to_have_value("Sent note")
            page.keyboard.press("Control+Enter")
            on_screen("editor")
            expect(page.get_by_label("Draft title", exact=True)).to_have_value("Sent note")
            page.keyboard.press("Escape")
            on_screen("drafts")
            expect(page.locator("#list-fired")).to_have_class("icon-button active")
            page.keyboard.press("Escape")
            on_screen("editor")
            expect(title).to_have_value("Groceries")

            # Escape while the speech model menu is open closes the menu and nothing else.
            page.locator("#voice-button").click(button="right")
            expect(page.locator("#voice-menu")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#voice-menu")).to_be_hidden()
            on_screen("editor")
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


MAC = """
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
Object.defineProperty(navigator, 'userAgentData', { get: () => ({ platform: 'macOS' }) });
"""


def fire_with_shortcut(tmp_path, monkeypatch, mac):
    """Write a note, move on to the destination screen and send it, all by keyboard; return what each step did."""
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    go, other = ("Meta+Enter", "Control+Enter") if mac else ("Control+Enter", "Meta+Enter")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            if mac:
                page.add_init_script(MAC)
            errors, fired = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def fire(route):
                fired.append(json.loads(route.request.post_data))
                route.fulfill(status=202, json={"id": "a-1", "title": "Ask", "body": "Hi", "state": "sending", "chat_id": "",
                                                "error": "", "destination": fired[-1]["destination"], "created_at": 1, "updated_at": 1})

            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
            page.route("**/api/machines", lambda route: route.fulfill(json={
                "machines": [{"id": "talker", "name": "primary-example"}], "current_machine_id": "talker", "unavailable": False}))
            page.route("**/api/chat-names?*", lambda route: route.fulfill(json={"chats": [{"id": "t-1", "name": "Talker chat"}]}))
            page.route("**/api/fire", fire)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            editor, setup = page.locator("#editor-view"), page.locator("#setup-view")
            page.get_by_label("Draft title", exact=True).fill("Ask")
            text = page.get_by_label("Draft text", exact=True)
            text.fill("Hi")
            # The hints name this system's key.
            expected = "Cmd+Enter" if mac else "Ctrl+Enter"
            expect(page.locator("#continue-button")).to_have_attribute("data-tip-more", re.compile(f"Shortcut: {re.escape(expected)}\\."))
            expect(page.locator("#preview-button")).to_have_attribute("data-tip-more", re.compile(f"Shortcut: {re.escape(expected)}\\."))
            # The other system's key does nothing, on either screen.
            text.click()
            page.keyboard.press(other)
            expect(editor).to_be_visible()
            page.keyboard.press(go)
            expect(setup).to_be_visible()
            page.locator("#follow-up").click()
            expect(page.locator("#target-chat option")).to_have_text(["Talker chat"])
            page.keyboard.press(other)
            expect(setup).to_be_visible()
            assert fired == []
            # The right key sends from the destination screen, and a fresh blank note comes up.
            page.keyboard.press(go)
            expect(editor).to_be_visible()
            expect(page.get_by_label("Draft title", exact=True)).to_have_value("")
            assert [item["destination"]["chat_id"] for item in fired] == ["t-1"]
            assert fired[0]["destination"]["mode"] == "follow-up"
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.browser
def test_ctrl_enter_moves_on_and_sends_by_keyboard(tmp_path, monkeypatch) -> None:
    fire_with_shortcut(tmp_path, monkeypatch, mac=False)


@pytest.mark.browser
def test_cmd_enter_moves_on_and_sends_by_keyboard_on_a_mac(tmp_path, monkeypatch) -> None:
    fire_with_shortcut(tmp_path, monkeypatch, mac=True)


@pytest.mark.browser
def test_pi_dev_picks_a_subscription_then_its_models_and_sends_its_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
            errors, fired = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def fire(route):
                fired.append(json.loads(route.request.post_data))
                route.fulfill(status=202, json={"id": "a-1", "title": "Ask", "body": "Hi", "state": "sending", "chat_id": "",
                                                "error": "", "destination": fired[-1]["destination"], "created_at": 1, "updated_at": 1})

            pi_choices = {
                "provider": "pi-dev", "available": True,
                "subscriptions": [
                    {"id": "opencode", "label": "OpenCode", "account_id": "", "available": False, "detail": "Not set up on this machine"},
                    {"id": "claude", "label": "Claude", "account_id": "claude-acct", "available": True, "detail": ""},
                    {"id": "codex", "label": "Codex (ChatGPT)", "account_id": "codex-acct", "available": True, "detail": ""},
                ],
                "models": [
                    {"id": "claude-code/claude-opus-5-5", "label": "Opus 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"], "subscription": "claude"},
                    {"id": "claude-code/claude-sonnet-5-5", "label": "Sonnet 5.5", "efforts": ["low", "medium", "high"], "subscription": "claude"},
                    {"id": "openai-codex/gpt-6-sol", "label": "GPT-6 Sol", "efforts": ["low", "medium", "high", "xhigh"], "subscription": "codex"},
                ],
            }
            page.route("**/api/chat-models/*", lambda route: route.fulfill(json=(
                pi_choices if "/pi-dev?" in route.request.url else {"provider": "claude", "available": True, "models": []})))
            page.route("**/api/machines", lambda route: route.fulfill(json={
                "machines": [{"id": "test", "name": "Test machine"}], "current_machine_id": "test", "unavailable": False}))
            page.route("**/api/fire", fire)
            page.route("**/api/fired", lambda route: route.fulfill(json={"actions": [
                {"id": "a-1", "title": "Ask", "body": "Hi", "state": "sending", "chat_id": "", "error": "",
                 "destination": fired[-1]["destination"], "created_at": 1, "updated_at": 1}
            ] if fired else []}))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_label("Draft title", exact=True).fill("Ask")
            page.get_by_label("Draft text", exact=True).fill("Hi")
            page.get_by_role("button", name="Continue", exact=True).click()
            # Other providers have no subscription to choose.
            expect(page.locator("#subscription-field")).to_be_hidden()
            page.locator("#provider").select_option("pi-dev")
            expect(page.locator("#subscription option")).to_have_text(["OpenCode (not set up)", "Claude", "Codex (ChatGPT)"])
            expect(page.locator("#subscription option").first).to_be_disabled()
            # The first subscription that is set up is chosen, and only its models are offered.
            expect(page.locator("#subscription")).to_have_value("claude")
            expect(page.locator("#model option")).to_have_text(["Opus 5.5", "Sonnet 5.5"])
            page.locator("#subscription").select_option("codex")
            expect(page.locator("#model option")).to_have_text(["GPT-6 Sol"])
            page.locator("#subscription").select_option("claude")
            page.locator("#model").select_option("claude-code/claude-opus-5-5")
            expect(page.locator("#effort option")).to_have_text(["low", "medium", "high", "xhigh", "max"])
            page.locator("#effort").select_option("high")
            page.locator("#preview-button").click()
            page.wait_for_function("document.getElementById('editor-view').hidden === false")
            destination = fired[0]["destination"]
            assert {key: destination[key] for key in ("provider", "subscription", "account_id", "model", "effort")} == {
                "provider": "pi-dev", "subscription": "claude", "account_id": "claude-acct",
                "model": "claude-code/claude-opus-5-5", "effort": "high"}
            page.locator("#fired-button").click()
            expect(page.locator(".fired-row").first).to_have_attribute("data-tip-more", re.compile(r"\(pi-dev Claude Opus 5\.5 high\)"))
            # The choice is remembered after queued writes reach the server.
            page.evaluate("defaultsWrite")
            page.reload()
            page.get_by_label("Draft text", exact=True).fill("Again")
            page.get_by_role("button", name="Continue", exact=True).click()
            expect(page.locator("#subscription")).to_have_value("claude")
            expect(page.locator("#model")).to_have_value("claude-code/claude-opus-5-5")
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.browser
def test_empty_untitled_drafts_vanish_when_left_and_are_trashed_without_asking(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            errors, dialogs = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def on_dialog(dialog):
                dialogs.append(dialog.message)
                dialog.accept()

            page.on("dialog", on_dialog)
            page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
            page.route("**/api/machines", lambda route: route.fulfill(json={"machines": [], "current_machine_id": "", "unavailable": True}))
            title, text = page.get_by_label("Draft title", exact=True), page.get_by_label("Draft text", exact=True)
            rows = page.locator("#draft-list .draft-row")

            def stored():
                return page.request.get(f"{base}/api/drafts").json()["drafts"]

            def until(check):
                for _ in range(100):
                    if check():
                        return
                    page.wait_for_timeout(50)
                assert check()

            # A note never written in is gone from the Drafts list as soon as the writer leaves it.
            page.goto(base + "/")
            page.get_by_role("button", name="Drafts", exact=True).first.click()
            expect(page.get_by_text("No drafts yet.")).to_be_visible()
            assert stored() == []

            # A note the writer emptied again after it was saved goes too, on the server as well.
            page.get_by_role("button", name="New draft", exact=True).click()
            text.fill("x")
            until(lambda: len(stored()) == 1)
            text.fill("")
            until(lambda: stored()[0]["body"] == "")
            page.get_by_role("button", name="Drafts", exact=True).first.click()
            expect(page.get_by_text("No drafts yet.")).to_be_visible()
            until(lambda: stored() == [])

            # A note with only a title, or only text, is kept; starting another note drops the empty one.
            page.get_by_role("button", name="New draft", exact=True).click()
            title.fill("Groceries")
            until(lambda: len(stored()) == 1)
            page.get_by_role("button", name="New draft", exact=True).first.click()
            page.get_by_role("button", name="Drafts", exact=True).first.click()
            expect(rows).to_have_count(1)
            expect(rows.first).to_have_text("Groceries")
            page.get_by_role("button", name="New draft", exact=True).click()
            text.fill("only text")
            until(lambda: len(stored()) == 2)
            page.get_by_role("button", name="Drafts", exact=True).first.click()
            expect(rows).to_have_count(2)
            assert dialogs == []

            # Empty untitled notes that are in the list anyway are trashed without a question; others still ask.
            for _ in range(2):
                page.request.post(base + "/api/drafts", data={"title": "", "body": ""})
            groceries = next(draft for draft in stored() if draft["title"] == "Groceries")
            page.request.patch(f"{base}/api/drafts/{groceries['id']}", data={"title": "Groceries", "body": "eggs"})
            page.reload()
            expect(title).to_have_value("Groceries")
            page.get_by_role("button", name="Drafts", exact=True).first.click()
            expect(rows).to_have_count(4)
            page.get_by_role("button", name="Delete Untitled", exact=True).first.click()
            expect(rows).to_have_count(3)
            page.get_by_role("button", name="Delete Untitled", exact=True).first.click()
            expect(rows).to_have_count(2)
            until(lambda: sorted(draft["title"] or draft["body"] for draft in stored()) == ["Groceries", "only text"])
            assert dialogs == []
            page.get_by_role("button", name="Delete Groceries", exact=True).click()
            expect(rows).to_have_count(1)
            assert dialogs == ["Delete “Groceries”? This cannot be undone."]
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
