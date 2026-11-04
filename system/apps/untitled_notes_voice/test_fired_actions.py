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


def test_a_new_chat_on_a_provider_default_model_is_created_without_a_model_pick(tmp_path: Path, monkeypatch) -> None:
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
    draft = client.post("/api/drafts", json={"title": "Agy note", "body": "Send to Agy"}).get_json()
    action = client.post("/api/fire", json={
        "draft_id": draft["id"],
        "destination": {"mode": "new", "machine": "fresh", "machine_name": "example-machine", "provider": "agy",
                        "model": runner.PROVIDER_DEFAULT_MODEL, "effort": "", "chat_name": "Agy note"},
    }).get_json()
    created = []

    def accepted(machine, method, path, payload=None, timeout=45):
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "agy-account", "harness": "antigravity", "provider": "Google"}]}
        created.append(payload)
        return 201, {"chat_id": "chat-1"}

    monkeypatch.setattr(runner, "chat_request", accepted)
    launched[0].target(*launched[0].args)
    assert len(created) == 1
    assert created[0]["account_id"] == "agy-account" and created[0]["message"] == "Send to Agy"
    assert "model" not in created[0]
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


def fire_new_chat(client, monkeypatch, title: str = "Studio check") -> dict:
    """A fired note whose delivery thread never runs, so a test sets the state it wants."""
    monkeypatch.setattr(runner.threading, "Thread", type("WaitingThread", (), {"__init__": lambda self, target, args, daemon: None, "start": lambda self: None}))
    draft = client.post("/api/drafts", json={"title": title, "body": "Keep these exact words"}).get_json()
    return client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "new", "machine": "worker", "machine_name": "secondary-example", "provider": "claude", "model": "sonnet", "chat_name": title,
    }}).get_json()


def age_action(tmp_path: Path, action_id: str, seconds: float, state: str = "checking", error: str = "") -> sqlite3.Row:
    with sqlite3.connect(tmp_path / "drafts.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE fired_actions SET state = ?, error = ?, created_at = created_at - ?, updated_at = updated_at - ? WHERE id = ?",
            (state, error, seconds, seconds, action_id),
        )
        return connection.execute("SELECT * FROM fired_actions WHERE id = ?", (action_id,)).fetchone()


def test_a_chat_that_exists_but_is_not_running_still_lands_after_the_wait(tmp_path: Path, monkeypatch) -> None:
    # Stuck for real: the chat was made, then stopped, and the note stayed "checking" for good.
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    action = fire_new_chat(client, monkeypatch)
    stopped = {"chats": [{"chat_id": "agent-1", "status": "stopped", "active_agent": {"state": "STOPPED"},
                          "labels": {runner.ACTION_LABEL: action["id"]}}]}
    monkeypatch.setattr(runner, "chat_request", lambda *args, **kwargs: (200, stopped))

    runner.reconcile_action(age_action(tmp_path, action["id"], 60))
    [waiting] = client.get("/api/fired").get_json()["actions"]
    assert waiting["state"] == "checking"

    runner.reconcile_action(age_action(tmp_path, action["id"], runner.CONFIRM_SECONDS))
    assert client.get("/api/fired").get_json()["actions"] == []
    with sqlite3.connect(tmp_path / "drafts.sqlite3") as connection:
        assert connection.execute("SELECT state, chat_id FROM fired_actions").fetchall() == [("landed", "agent-1")]


def test_an_unreachable_machine_ends_in_attention_with_the_reason(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    action = fire_new_chat(client, monkeypatch)

    def unreachable(*args, **kwargs):
        raise OSError("Selected machine did not respond")

    monkeypatch.setattr(runner, "chat_request", unreachable)
    runner.reconcile_action(age_action(tmp_path, action["id"], 60, error="Selected machine did not respond"))
    assert client.get("/api/fired").get_json()["actions"][0]["state"] == "checking"

    runner.reconcile_action(age_action(tmp_path, action["id"], runner.CONFIRM_SECONDS, error="Selected machine did not respond"))
    [stuck] = client.get("/api/fired").get_json()["actions"]
    assert stuck["state"] == "attention"
    assert stuck["error"].startswith("Delivery could not be confirmed.")
    assert stuck["error"].endswith("Last problem: Selected machine did not respond")
    # Every state change is in the log under the send's id.
    logged = capsys.readouterr().err
    assert f"send {action['id']} cannot be confirmed yet: Selected machine did not respond" in logged
    assert f"send {action['id']} now attention" in logged


def test_a_stuck_send_can_be_dismissed_and_stays_dismissed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    action = fire_new_chat(client, monkeypatch)
    assert client.delete(f"/api/fired/{action['id']}").status_code == 409   # still sending
    assert client.delete("/api/fired/no-such-send").status_code == 404

    age_action(tmp_path, action["id"], 30, state="attention", error="Delivery could not be confirmed.")
    assert client.delete(f"/api/fired/{action['id']}").get_json() == {"dismissed": action["id"]}
    assert client.get("/api/fired").get_json()["actions"] == []
    assert client.delete(f"/api/fired/{action['id']}").status_code == 404

    # A delivery that answers late does not bring it back, and the record is kept.
    runner.set_action(action["id"], "attention", error="late")
    assert client.get("/api/fired").get_json()["actions"] == []
    with sqlite3.connect(tmp_path / "drafts.sqlite3") as connection:
        assert connection.execute("SELECT state, body FROM fired_actions").fetchall() == [("dismissed", "Keep these exact words")]





def test_resend_is_fresh_preserves_original_and_prevents_duplicate_clicks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "deliver_action", lambda action_id: None)
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "A failed note", "body": "Exact words\nSecond line"}).get_json()
    destination = {"mode": "follow-up", "machine": "worker", "chat_id": "target"}
    original = client.post("/api/fire", json={"draft_id": draft["id"], "destination": destination}).get_json()
    runner.set_action(original["id"], "attention", error="Refused")
    retry = client.post(f"/api/fired/{original['id']}/resend").get_json()
    assert retry["id"] != original["id"]
    assert retry["retry_of"] == retry["retry_root_id"] == original["id"]
    assert retry["body"] == original["body"] and retry["destination"] == destination
    assert retry["state"] == "sending" and retry["chat_id"] == retry["error"] == ""
    assert client.post(f"/api/fired/{original['id']}/resend").get_json()["id"] == retry["id"]
    assert len(client.get("/api/fired").get_json()["actions"]) == 2
    runner.set_action(retry["id"], "attention", error="Still refused")
    saved = {a["id"]: a for a in client.get("/api/fired").get_json()["actions"]}
    assert saved[original["id"]]["error"] == "Refused"
    assert saved[original["id"]]["state"] == "attention"
    next_retry = client.post(f"/api/fired/{retry['id']}/resend").get_json()
    assert next_retry["id"] not in saved
    assert next_retry["retry_of"] == retry["id"]
    assert next_retry["retry_root_id"] == original["id"]
    runner.set_action(next_retry["id"], "landed", chat_id="target")
    assert client.get("/api/fired").get_json()["actions"] == []
    # A late answer from an older delivery cannot bring it back into the outbox.
    runner.set_action(retry["id"], "checking", error="Late timeout")
    assert client.get("/api/fired").get_json()["actions"] == []
    with runner.database() as connection:
        rows = connection.execute("SELECT * FROM fired_actions").fetchall()
    assert len(rows) == 3
    assert all(row["body"] == original["body"] for row in rows)
    assert {row["state"] for row in rows} == {"landed", "superseded"}


def test_resend_uses_normal_delivery_with_fresh_identity(tmp_path, monkeypatch) -> None:
    deliver = runner.deliver_action
    queued = []
    monkeypatch.setattr(runner, "deliver_action", queued.append)
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "New chat", "body": "Unchanged prompt"}).get_json()
    original = client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "new", "machine": "worker", "provider": "claude", "model": "sonnet", "effort": "high",
    }}).get_json()
    assert client.post(f"/api/fired/{original['id']}/resend").status_code == 409
    runner.set_action(original["id"], "attention", chat_id="old-chat", error="Failed")
    retry = client.post(f"/api/fired/{original['id']}/resend").get_json()
    assert retry["chat_id"] == ""
    assert retry["id"] != original["id"] and retry["retry_of"] == original["id"]
    runner.set_action(retry["id"], "checking", error="Response lost")
    assert len(client.get("/api/fired").get_json()["actions"]) == 2
    calls = []
    def accepted(machine, method, path, payload=None, timeout=45):
        calls.append((machine, method, path, payload))
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "connected", "harness": "claude"}]}
        return 201, {"chat_id": "fresh-chat"}
    monkeypatch.setattr(runner, "chat_request", accepted)
    deliver(retry["id"])
    machine, method, path, payload = calls[-1]
    assert (machine, method, path) == ("worker", "POST", "/api/chats/create")
    assert payload["message"] == original["body"]
    assert payload["labels"][runner.ACTION_LABEL] == retry["id"]
    assert payload["model"] == {"model_id": "sonnet", "effort": "high", "fast": False}
    assert client.post(f"/api/fired/{original['id']}/resend").status_code == 404
    assert client.post("/api/fired/missing/resend").status_code == 404
