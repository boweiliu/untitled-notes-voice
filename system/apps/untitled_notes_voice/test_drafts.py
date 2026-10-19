import json
import sqlite3
import threading
from pathlib import Path

from prompt_notes import runner


def test_multiple_drafts_persist_and_update_independently(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    first_client = runner.app.test_client()

    first = first_client.post("/api/drafts", json={"title": "Idea one", "body": "First prompt"}).get_json()
    second = first_client.post("/api/drafts", json={"title": "Idea two", "body": "Second prompt"}).get_json()
    assert first is not None and second is not None

    updated = first_client.patch(
        f"/api/drafts/{first['id']}", json={"title": "Revised idea", "body": "Revised prompt"}
    ).get_json()
    assert updated is not None and updated["title"] == "Revised idea"

    later_client = runner.app.test_client()
    drafts = later_client.get("/api/drafts").get_json()["drafts"]
    assert [(draft["id"], draft["title"], draft["body"]) for draft in drafts] == [
        (first["id"], "Revised idea", "Revised prompt"),
        (second["id"], "Idea two", "Second prompt"),
    ]
    assert (tmp_path / "drafts.sqlite3").is_file()


def test_existing_drafts_keep_their_text_and_gain_titles(tmp_path: Path, monkeypatch) -> None:
    with sqlite3.connect(tmp_path / "drafts.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE drafts (id TEXT PRIMARY KEY, body TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO drafts VALUES (?, ?, ?, ?)", ("old-draft", "Older idea\nkeep this", 1, 1)
        )

    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    [draft] = runner.app.test_client().get("/api/drafts").get_json()["drafts"]
    assert draft["id"] == "old-draft"
    assert draft["title"] == "Older idea"
    assert draft["body"] == "Older idea\nkeep this"


def test_empty_title_stays_empty_and_destination_defaults_persist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"body": "A new idea"}).get_json()
    assert draft["title"] == ""
    assert client.patch(
        f"/api/drafts/{draft['id']}", json={"title": "", "body": "A revised idea"}
    ).get_json()["title"] == ""

    preferences = {
        "machine": "agent-remote",
        "machine_name": "Writing machine",
        "provider": "codex",
        "by_provider": {
            "claude": {"model": "sonnet", "effort_by_model": {"sonnet": "high"}},
            "codex": {"model": "gpt-6-sol", "effort_by_model": {"gpt-6-sol": "medium"}},
        },
    }
    assert client.put("/api/destination-defaults", json=preferences).status_code == 200
    assert runner.app.test_client().get("/api/destination-defaults").get_json() == preferences


def test_machine_choices_show_running_linked_machines_and_current_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-local")
    monkeypatch.setattr(runner, "studio_workspaces", lambda: {"workspaces": [
        {"agent_id": "agent-remote", "name": "Writing machine", "host_state": "RUNNING"},
        {"agent_id": "agent-stopped", "name": "Old machine", "host_state": "STOPPED"},
    ]})
    response = runner.app.test_client().get("/api/machines").get_json()
    assert response == {
        "current_machine_id": "agent-local",
        "machines": [
            {"id": "agent-local", "name": "This machine"},
            {"id": "agent-remote", "name": "Writing machine"},
        ],
        "unavailable": False,
    }

    # When the machine list cannot be read, the last good one stays, even for a refresh.
    monkeypatch.setattr(runner, "studio_workspaces", lambda: None)
    assert runner.app.test_client().get("/api/machines").get_json() == response
    assert runner.app.test_client().get("/api/machines?fresh=1").get_json() == response

    # With nothing remembered, it says so.
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path / "elsewhere")
    assert runner.app.test_client().get("/api/machines").get_json() == {
        "current_machine_id": "agent-local",
        "machines": [{"id": "agent-local", "name": "This machine"}],
        "unavailable": True,
    }


def test_chat_choices_use_catalog_account_options_and_chat_names(monkeypatch) -> None:
    def fake_chat_json(path: str):
        if path == "/api/harnesses":
            return {
                "claude": {"options": [
                    {"id": "sonnet", "label": "Sonnet", "in_picker": True,
                     "efforts": [{"level": "high", "in_picker": True}]},
                    {"id": "hidden", "label": "Hidden", "in_picker": False, "efforts": []},
                ]},
                "codex": {"options": []},
            }
        if path == "/api/accounts":
            return {"accounts": [{"id": "codex-account", "harness": "codex"}]}
        if path == "/api/accounts/codex-account/model-options":
            return {"options": [{"id": "gpt-6-sol", "label": "GPT-6-Sol", "in_picker": True,
                                 "efforts": [{"level": "low", "in_picker": True}]}]}
        if path == "/api/chats":
            return {"chats": [{"chat_id": "two", "title": "Zebra"},
                              {"chat_id": "one", "name": "Apple"}]}
        raise AssertionError(path)

    monkeypatch.setattr(runner, "chat_json", fake_chat_json)
    client = runner.app.test_client()
    assert client.get("/api/chat-models/claude").get_json()["models"] == [
        {"id": "sonnet", "label": "Sonnet", "efforts": ["high"]}
    ]
    assert client.get("/api/chat-models/codex").get_json()["models"] == [
        {"id": "gpt-6-sol", "label": "GPT-6-Sol", "efforts": ["low"]}
    ]
    assert client.get("/api/chat-names").get_json()["chats"] == [
        {"id": "one", "name": "Apple"}, {"id": "two", "name": "Zebra"}
    ]


def test_delete_only_selected_draft_and_preserve_fired_history(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    first = client.post("/api/drafts", json={"title": "Remove", "body": "Old text"}).get_json()
    keep = client.post("/api/drafts", json={"title": "Keep", "body": "Important text"}).get_json()
    with runner.database() as connection:
        connection.execute(
            "INSERT INTO fired_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("history", first["id"], "Sent", "Sent original", "{}", "attention", "", "", 1, 1),
        )
    assert client.delete(f"/api/drafts/{first['id']}").status_code == 200
    assert client.delete(f"/api/drafts/{first['id']}").status_code == 404
    assert client.get("/api/drafts").get_json()["drafts"] == [keep]
    assert client.get("/api/fired").get_json()["actions"][0]["body"] == "Sent original"


def test_models_come_from_selected_destination(monkeypatch):
    monkeypatch.setenv("MNGR_AGENT_ID", "local")
    calls = []
    def remote(machine, method, path, payload=None, timeout=45):
        calls.append((machine, method, path))
        if path == "/api/harnesses":
            return 200, {"codex": {"options": []}}
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "remote-account", "harness": "codex"}]}
        assert path == "/api/accounts/remote-account/model-options"
        return 200, {"options": [{"id": "remote-model", "label": "Remote model", "in_picker": True, "efforts": []}]}
    monkeypatch.setattr(runner, "chat_request", remote)
    response = runner.app.test_client().get("/api/chat-models/codex?machine=worker").get_json()
    assert response["models"][0]["id"] == "remote-model"
    assert response["available"] is True
    assert all(machine == "worker" and method == "GET" for machine, method, path in calls)


def test_choices_answer_from_memory_and_refresh_behind(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MNGR_AGENT_ID", "local")
    state = {"model": "first", "fail": False}
    calls = []
    gate = threading.Event()
    gate.set()

    def remote(machine, method, path, payload=None, timeout=45):
        calls.append(path)
        if state["fail"]:
            raise OSError("machine is off")
        if path == "/api/harnesses":
            return 200, {"codex": {"options": []}}
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "remote-account", "harness": "codex"}]}
        assert gate.wait(10)
        return 200, {"options": [{"id": state["model"], "label": "Model", "in_picker": True, "efforts": []}]}

    def model_ids(query: str = "") -> list[str]:
        response = client.get(f"/api/chat-models/codex?machine=worker{query}")
        assert response.status_code == 200
        return [model["id"] for model in response.get_json()["models"]]

    monkeypatch.setattr(runner, "chat_request", remote)
    client = runner.app.test_client()
    assert model_ids() == ["first"]
    assert len(calls) == 3
    # Asked again soon after: answered from memory, the machine is not asked.
    assert model_ids() == ["first"]
    assert len(calls) == 3

    # Once the answer is old, it still comes back at once while the machine is slow,
    # and a background refresh picks up the change for the next ask.
    monkeypatch.setattr(runner, "CHOICE_REFRESH_SECONDS", 0.0)
    refreshed = threading.Event()
    remember = runner.remember_choice
    monkeypatch.setattr(runner, "remember_choice", lambda *args: (remember(*args), refreshed.set()))
    state["model"] = "second"
    gate.clear()
    assert model_ids() == ["first"]
    gate.set()
    assert refreshed.wait(10)
    monkeypatch.setattr(runner, "CHOICE_REFRESH_SECONDS", 3600.0)
    assert model_ids() == ["second"]

    # A restarted app answers from the saved copy without asking the machine.
    runner.CHOICE_CACHES.clear()
    asked = len(calls)
    assert model_ids() == ["second"]
    assert len(calls) == asked
    saved = json.loads((tmp_path / "choice_cache.json").read_text())
    assert saved["models:worker:codex"]["value"]["models"][0]["id"] == "second"

    # A refresh that cannot reach the machine keeps the last answer.
    state["fail"] = True
    assert model_ids("&fresh=1") == ["second"]
    assert len(calls) > asked

    # Chat lists are remembered per machine the same way; a refresh asks again.
    state["fail"] = False
    chats = {"worker": [{"chat_id": "w-1", "title": "First chat"}]}
    monkeypatch.setattr(runner, "chat_request", lambda machine, method, path, payload=None, timeout=45: (200, {"chats": chats[machine]}))
    assert client.get("/api/chat-names?machine=worker").get_json()["chats"] == [{"id": "w-1", "name": "First chat"}]
    chats["worker"].append({"chat_id": "w-2", "title": "New chat"})
    assert len(client.get("/api/chat-names?machine=worker").get_json()["chats"]) == 1
    assert len(client.get("/api/chat-names?machine=worker&fresh=1").get_json()["chats"]) == 2
