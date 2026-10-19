from pathlib import Path
import sqlite3

from prompt_notes import runner


def test_fire_preserves_text_until_chat_confirms_delivery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MNGR_AGENT_ID", "local-machine")
    launched = []

    class WaitingThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            launched.append(self)

    monkeypatch.setattr(runner.threading, "Thread", WaitingThread)
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "Market map", "body": "Original private prompt"}).get_json()
    action = client.post("/api/fire", json={
        "draft_id": draft["id"],
        "destination": {"mode": "new", "machine": "local-machine", "machine_name": "This machine",
                        "provider": "claude", "model": "sonnet", "effort": "high", "chat_name": "Market map"},
    }).get_json()
    assert action["state"] == "sending" and action["body"] == "Original private prompt"
    assert client.get("/api/drafts").get_json()["drafts"] == []
    assert client.get("/api/fired").get_json()["actions"][0]["body"] == "Original private prompt"

    def accepted(machine, method, path, payload: dict | None = None, timeout=45):
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "account-1", "harness": "claude"}]}
        assert payload is not None
        assert payload["message"] == "Original private prompt"
        assert payload["labels"][runner.ACTION_LABEL] == action["id"]
        assert payload["model"] == {"model_id": "sonnet", "effort": "high", "fast": False}
        return 201, {"chat_id": "chat-1"}

    monkeypatch.setattr(runner, "chat_request", accepted)
    launched[0].target(*launched[0].args)
    assert client.get("/api/fired").get_json()["actions"] == []


def test_uncertain_send_keeps_original_text_and_never_retries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MNGR_AGENT_ID", "local-machine")
    monkeypatch.setattr(runner.threading, "Thread", type("WaitingThread", (), {"__init__": lambda self, target, args, daemon: None, "start": lambda self: None}))
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "", "body": "Keep these exact words"}).get_json()
    action = client.post("/api/fire", json={
        "draft_id": draft["id"], "destination": {"mode": "follow-up", "chat_id": "chat-1"},
    }).get_json()
    calls = []

    def timed_out(machine, method, path, payload=None, timeout=45):
        calls.append(path)
        raise OSError("response lost")

    monkeypatch.setattr(runner, "chat_request", timed_out)
    runner.deliver_action(action["id"])
    [saved] = client.get("/api/fired").get_json()["actions"]
    assert saved["state"] == "checking" and saved["body"] == "Keep these exact words"
    assert calls == ["/api/chats/chat-1/message"]


def test_slow_send_checks_then_rejection_needs_attention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner.threading, "Thread", type("WaitingThread", (), {"__init__": lambda self, target, args, daemon: None, "start": lambda self: None}))
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "Follow up", "body": "Keep the wording"}).get_json()
    action = client.post("/api/fire", json={
        "draft_id": draft["id"], "destination": {"mode": "follow-up", "chat_id": "chat-1"},
    }).get_json()
    with sqlite3.connect(tmp_path / "drafts.sqlite3") as connection:
        connection.execute("UPDATE fired_actions SET updated_at = updated_at - 11 WHERE id = ?", (action["id"],))
    [slow] = client.get("/api/fired").get_json()["actions"]
    assert slow["state"] == "checking" and slow["body"] == "Keep the wording"

    monkeypatch.setattr(runner, "chat_request", lambda *args, **kwargs: (409, {"detail": "Chat refused the message"}))
    runner.deliver_action(action["id"])
    [rejected] = client.get("/api/fired").get_json()["actions"]
    assert rejected["state"] == "attention"
    assert rejected["error"] == "Chat refused the message"
    assert rejected["body"] == "Keep the wording"


def test_follow_up_lists_and_delivers_to_the_selected_remote_machine(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MNGR_AGENT_ID", "talker")
    monkeypatch.setattr(runner.threading, "Thread", type("WaitingThread", (), {"__init__": lambda self, target, args, daemon: None, "start": lambda self: None}))
    calls = []

    def remote(machine, method, path, payload=None, timeout=45):
        calls.append((machine, method, path, payload))
        if path == "/api/chats":
            return 200, {"chats": [{"chat_id": "w-2", "title": "Zeta"}, {"chat_id": "w-1", "name": "Alpha"}]}
        return 202, {}

    monkeypatch.setattr(runner, "chat_request", remote)
    monkeypatch.setattr(runner, "chat_json", lambda path: {"chats": [{"chat_id": "local-1", "title": "Here"}]})
    client = runner.app.test_client()
    assert client.get("/api/chat-names?machine=worker").get_json()["chats"] == [{"id": "w-1", "name": "Alpha"}, {"id": "w-2", "name": "Zeta"}]
    assert client.get("/api/chat-names?machine=talker").get_json()["chats"] == [{"id": "local-1", "name": "Here"}]
    assert client.get("/api/chat-names").get_json()["chats"] == [{"id": "local-1", "name": "Here"}]
    assert calls == [("worker", "GET", "/api/chats", None)]

    draft = client.post("/api/drafts", json={"title": "Ask worker", "body": "Please check the logs"}).get_json()
    action = client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "follow-up", "machine": "worker", "machine_name": "Worker machine", "chat_id": "w-1", "chat_name": "Alpha",
    }}).get_json()
    runner.deliver_action(action["id"])
    assert calls[-1] == ("worker", "POST", "/api/chats/w-1/message", {"message": "Please check the logs", "message_id": action["id"]})
    assert client.get("/api/fired").get_json()["actions"] == []


def test_remote_chat_list_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MNGR_AGENT_ID", "talker")

    def unreachable(*args, **kwargs):
        raise OSError("Selected machine did not respond")

    monkeypatch.setattr(runner, "chat_request", unreachable)
    response = runner.app.test_client().get("/api/chat-names?machine=worker")
    assert response.status_code == 503


def test_follow_up_requires_a_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "", "body": "Text"}).get_json()
    for destination in ({"mode": "follow-up", "chat_id": ""}, {"mode": "follow-up", "chat_id": "c", "machine": 3}):
        assert client.post("/api/fire", json={"draft_id": draft["id"], "destination": destination}).status_code == 400
