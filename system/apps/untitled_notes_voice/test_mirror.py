"""A machine that follows another machine's notes (secondary-example follows primary-example) is a view-only copy."""
import threading
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from prompt_notes import runner

DEPLOY = Path(__file__).parent / "deploy"




def make_copy(data_dir: Path, url: str = "https://notes.example.test/") -> None:
    (data_dir / "mirror.json").write_text(json.dumps({"source": "primary-example", "source_url": url}))


def test_a_copy_shows_its_notes_but_refuses_every_change(tmp_path: Path) -> None:
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "Real note", "body": "Keep me"}).get_json()
    make_copy(tmp_path)

    assert [d["body"] for d in client.get("/api/drafts").get_json()["drafts"]] == ["Keep me"]
    assert client.get("/api/fired").status_code == 200
    attempts = [
        client.post("/api/drafts", json={"body": "New"}),
        client.patch(f"/api/drafts/{draft['id']}", json={"body": "Changed"}),
        client.delete(f"/api/drafts/{draft['id']}"),
        client.post("/api/fire", json={"draft_id": draft["id"]}),
        client.put("/api/destination-defaults", json={}),
        client.delete("/api/fired/some-id"),
    ]
    for response in attempts:
        assert response.status_code == 409
        assert "view-only copy of the notes on primary-example" in response.get_json()["description"]
    [kept] = client.get("/api/drafts").get_json()["drafts"]
    assert (kept["id"], kept["body"]) == (draft["id"], "Keep me")


def test_an_unreadable_copy_marker_still_refuses_changes(tmp_path: Path) -> None:
    (tmp_path / "mirror.json").write_text("{not json")
    response = runner.app.test_client().post("/api/drafts", json={"body": "New"})
    assert response.status_code == 409
    assert "the main machine" in response.get_json()["description"]


def test_the_page_says_it_is_a_copy_and_links_to_the_real_notes(tmp_path: Path) -> None:
    client = runner.app.test_client()
    assert 'id="mirror-notice"' not in client.get("/").get_data(as_text=True)
    make_copy(tmp_path)
    page = client.get("/").get_data(as_text=True)
    assert 'id="mirror-notice"' in page
    assert '<a href="https://notes.example.test/" target="_blank" rel="noopener">Open the notes on primary-example</a>' in page
    make_copy(tmp_path, url="javascript:alert(1)")
    page = client.get("/").get_data(as_text=True)
    assert "javascript:" not in page and 'id="mirror-notice"' in page


def test_a_copy_never_checks_on_sends(tmp_path: Path, monkeypatch) -> None:
    with runner.database() as connection:
        connection.execute(
            "INSERT INTO fired_actions (id, draft_id, title, body, destination, state, created_at, updated_at) "
            "VALUES ('a1', 'd1', 't', 'b', '{}', 'sending', 1, 1)"
        )
    make_copy(tmp_path)

    class Stop(Exception):
        pass

    def stop(_seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(runner, "reconcile_action", lambda row: pytest.fail("a copy reconciled a send"))
    monkeypatch.setattr(runner.time, "sleep", stop)
    with pytest.raises(Stop):
        runner.check_uncertain_actions()
    with runner.database() as connection:
        assert connection.execute("SELECT state FROM fired_actions WHERE id = 'a1'").fetchone()[0] == "sending"








@pytest.mark.browser
def test_a_copy_in_a_real_browser_reads_but_offers_no_writing_or_sending(tmp_path: Path) -> None:


    with runner.database() as connection:
        connection.execute(
            "INSERT INTO drafts (id, title, body, created_at, updated_at) VALUES ('d1', 'From talker', 'Real words', 1, 2)"
        )
        connection.execute(
            "INSERT INTO fired_actions (id, draft_id, title, body, destination, state, created_at, updated_at) "
            "VALUES ('a1', 'd1', 'Sent one', 'Sent words', '{\"mode\": \"new\"}', 'attention', 1, 1)"
        )
    make_copy(tmp_path)
    before = (tmp_path / "drafts.sqlite3").read_bytes()
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    requests: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda req: requests.append(f"{req.method} {req.url}"))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            notice = page.locator("#mirror-notice")
            expect(notice).to_contain_text("view-only copy of the notes on primary-example")
            expect(notice.get_by_role("link", name="Open the notes on primary-example")).to_have_attribute("href", "https://notes.example.test/")
            expect(page.locator("#draft-title")).to_have_value("From talker")
            expect(page.locator("#draft-text")).to_have_value("Real words")
            expect(page.locator("#draft-text")).not_to_be_editable()
            expect(page.locator("#draft-title")).not_to_be_editable()
            for hidden in ("#continue-button", "#voice-button", ".new-draft-button >> nth=0"):
                expect(page.locator(hidden)).to_be_hidden()
            page.locator("#draft-text").press("Control+Enter")
            expect(page.locator("#setup-view")).to_be_hidden()

            page.locator("#drafts-button").click()
            expect(page.locator(".draft-row")).to_have_count(1)
            expect(page.locator("#draft-list .trash-button")).to_be_hidden()
            page.locator("#list-fired").click()
            expect(page.locator(".fired-row")).to_have_count(1)
            expect(page.locator("#draft-list .trash-button")).to_be_hidden()
            page.locator(".fired-row").click()
            expect(page.locator("#draft-text")).to_have_value("Sent words")
            for hidden in ("#resend-button", "#remove-fired-button"):
                expect(page.locator(hidden)).to_be_hidden()
            expect(page.locator("#copy-button")).to_be_visible()
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
    changing = [r for r in requests if not r.startswith("GET ")]
    assert changing == []
    assert not any("/api/chat-models/" in r for r in requests)
    assert (tmp_path / "drafts.sqlite3").read_bytes() == before
