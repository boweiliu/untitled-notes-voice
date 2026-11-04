"""The Fired list in a real browser: what each icon means, the ID on a sent note, and removing stuck ones."""
import json
import sqlite3
import threading
import time

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from prompt_notes import runner

FORTRESS = "/opt/fortress/tilion-fortress/tilion"
STUCK_ID = "00000000-0000-4000-8000-000000000001"
LOST_ID = "00000000-0000-4000-8000-000000000002"


def seed(data_dir):
    destination = json.dumps({"mode": "new", "chat_name": "sample-conversation", "machine": "worker", "machine_name": "secondary-example",
                              "provider": "claude", "model": "sonnet", "effort": "high"})
    now = time.time()
    rows = [
        (STUCK_ID, "Sample delivery", "checking", "Selected machine did not respond", now - 1800),
        (LOST_ID, "Sample advice", "attention", "Delivery could not be confirmed. Your text is safe here.", now - 2400),
    ]
    with runner.database() as connection:
        for action_id, title, state, error, created in rows:
            connection.execute(
                "INSERT INTO fired_actions (id, draft_id, title, body, destination, state, error, created_at, updated_at) "
                "VALUES (?, 'draft', ?, 'The exact words I wrote', ?, ?, ?, ?, ?)",
                (action_id, title, destination, state, error, created, created + 5),
            )


def states(data_dir):
    with sqlite3.connect(data_dir / "drafts.sqlite3") as connection:
        return dict(connection.execute("SELECT id, state FROM fired_actions").fetchall())


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    seed(tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, tmp_path
    server.shutdown()


def open_page(playwright, server, **context):
    browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
    page = browser.new_page(**context)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/chat-models/*", lambda route: route.fulfill(json={"provider": "claude", "available": True, "models": []}))
    page.route("**/api/machines", lambda route: route.fulfill(json={"machines": [], "current_machine_id": "", "unavailable": True}))
    page.goto(f"http://127.0.0.1:{server.server_port}/")
    return browser, page, errors


@pytest.mark.browser
def test_hover_explains_icons_and_a_sent_note_shows_its_id(served) -> None:
    server, data_dir = served
    with sync_playwright() as playwright:
        browser, page, errors = open_page(playwright, server, viewport={"width": 1100, "height": 800})
        tip = page.locator("#tip")
        # Leaving an icon before its hint has appeared cancels the hint.
        page.locator("#fired-button").hover()
        page.mouse.move(5, 400)
        page.wait_for_timeout(400)
        expect(tip).to_be_hidden()
        page.locator("#fired-button").hover()
        expect(tip).to_be_visible()
        expect(tip).to_contain_text("Notes you have sent, and whether each one arrived.")
        page.mouse.move(5, 400)
        expect(tip).to_be_hidden()

        page.locator("#fired-button").click()
        stuck = page.locator(f'.draft-entry[data-id="{STUCK_ID}"]')
        lost = page.locator(f'.draft-entry[data-id="{LOST_ID}"]')
        lost.locator(".fired-status").hover()
        expect(tip).to_contain_text("Needs attention")
        expect(tip).to_contain_text("use Resend if it never arrived")
        stuck.locator(".fired-status").hover()
        expect(tip).to_contain_text("Checking")
        expect(tip).to_contain_text("Nothing to do yet")
        # The list is redrawn only when something changed, so a hint is not wiped while it is read.
        page.wait_for_timeout(3200)
        expect(tip).to_be_visible()
        # Keyboard focus explains an icon too.
        page.locator("#list-drafts").focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        expect(tip).to_contain_text("Drafts")

        stuck.locator(".fired-row").click()
        info = page.locator("#fired-info")
        expect(info).to_be_visible()
        expect(info).to_contain_text("Checking")
        expect(info).to_contain_text("Sent to new chat “sample-conversation” on secondary-example (claude sonnet high)")
        expect(info).to_contain_text("Last problem: Selected machine did not respond")
        expect(page.locator(".id-chip")).to_have_text(STUCK_ID)
        page.locator(".id-chip").click()
        expect(page.locator(".fired-info .copied")).to_have_text("Copied")
        expect(page.locator("#draft-text")).to_have_value("The exact words I wrote")
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_stuck_sends_can_be_removed_from_the_list_and_from_their_page(served) -> None:
    server, data_dir = served
    with sync_playwright() as playwright:
        browser, page, errors = open_page(playwright, server, viewport={"width": 1100, "height": 800})
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#fired-button").click()
        entries = page.locator("#draft-list .draft-entry")
        expect(entries).to_have_count(2)

        page.get_by_role("button", name="Remove Sample delivery from Fired").click()
        expect(entries).to_have_count(1)
        assert states(data_dir)[STUCK_ID] == "dismissed"

        entries.first.locator(".fired-row").click()
        page.get_by_role("button", name="Remove from Fired").click()
        expect(page.locator("#drafts-view")).to_be_visible()
        expect(page.locator("#draft-list .draft-entry")).to_have_count(0)
        expect(page.get_by_label("Outbox empty")).to_be_visible()
        assert states(data_dir) == {STUCK_ID: "dismissed", LOST_ID: "dismissed"}
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_phone_spells_out_what_icons_mean_and_press_and_hold_explains_the_rest(served) -> None:
    server, data_dir = served
    with sync_playwright() as playwright:
        browser, page, errors = open_page(playwright, server, viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
        page.locator("#fired-button").tap()
        caption = page.locator(f'.draft-entry[data-id="{LOST_ID}"] .fired-caption')
        expect(caption).to_be_visible()
        expect(caption).to_contain_text("Needs attention: not confirmed · secondary-example ·")
        expect(page.locator(f'.draft-entry[data-id="{STUCK_ID}"] .fired-caption')).to_contain_text("Checking: no reply yet")

        # Press and hold shows the hint and does not also press the button.
        box = page.locator("#list-drafts").bounding_box()
        cdp = page.context.new_cdp_session(page)
        point = [{"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}]
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point})
        page.wait_for_timeout(700)
        expect(page.locator("#tip")).to_contain_text("Notes you are still writing.")
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(200)
        expect(page.locator("#list-fired")).to_have_class("icon-button active")   # still on Fired: the hold was not a tap
        # A plain tap still works, and hides the hint.
        page.locator("#list-drafts").tap()
        expect(page.locator("#list-drafts")).to_have_class("icon-button active")
        expect(page.locator("#tip")).to_be_hidden()
        assert errors == []
        browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [390, 1100])
def test_resend_keeps_original_until_fresh_attempt_is_confirmed(served, monkeypatch, width) -> None:
    server, data_dir = served
    monkeypatch.setattr(runner, "deliver_action", lambda action_id: None)
    with sync_playwright() as playwright:
        browser, page, errors = open_page(playwright, server, viewport={"width": width, "height": 844})
        page.get_by_role("button", name="Fired", exact=True).click()
        page.locator(f'.draft-entry[data-id="{LOST_ID}"] .fired-row').click()
        page.get_by_role("button", name="Resend", exact=True).click()
        expect(page.locator("#fired-info")).to_contain_text(f"Resent from {LOST_ID}")
        expect(page.locator("#draft-text")).to_have_value("The exact words I wrote")
        with runner.database() as connection:
            retry = connection.execute("SELECT * FROM fired_actions WHERE retry_of = ?", (LOST_ID,)).fetchone()
        assert retry is not None and retry["id"] != LOST_ID
        page.get_by_role("button", name="Fired", exact=True).click()
        expect(page.locator(".fired-row")).to_have_count(3)
        page.locator(f'.draft-entry[data-id="{LOST_ID}"] .fired-row').click()
        expect(page.get_by_role("button", name="Resend", exact=True)).to_be_disabled()
        runner.set_action(retry["id"], "attention", error="Retry refused")
        expect(page.get_by_role("button", name="Resend", exact=True)).to_be_enabled()
        assert states(data_dir)[LOST_ID] == "attention"
        runner.set_action(retry["id"], "landed", chat_id="confirmed-chat")
        page.get_by_role("button", name="Fired", exact=True).click()
        expect(page.locator(".fired-row")).to_have_count(1)
        assert states(data_dir)[LOST_ID] == "superseded"
        assert errors == []
        browser.close()
