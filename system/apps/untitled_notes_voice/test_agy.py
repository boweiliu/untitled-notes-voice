"""Agy's model list, the plan for reaching a model in its terminal picker, and the note's delivery to it."""
from typing import Any

import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from prompt_notes import agy_models, agy_remote, runner

AGY_MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
    "gemini-4-argon-high\tGemini 4 Argon (High)\n"
    "gemini-4-argon-low\tGemini 4 Argon (Low)\n"
    "claude-opus-5-5-low\tClaude Opus 5.5 (Low)\n"
    "claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)\n"
    "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)
ROWS = agy_models.parse_models(AGY_MODELS_OUTPUT)


def test_models_are_read_from_agy_output_without_its_progress_line() -> None:
    assert ROWS[0] == {"id": "gemini-4-argon-medium", "label": "Gemini 4 Argon (Medium)"}
    assert len(ROWS) == 7
    assert agy_models.parse_models("Fetching available models...\n") == []


@pytest.mark.parametrize("label, expected", [
    ("Claude Opus 5.5 (High)", {"search": "Claude Opus 5.5", "levels": 3, "effort_index": 2}),
    ("Claude Opus 5.5 (Low)", {"search": "Claude Opus 5.5", "levels": 3, "effort_index": 0}),
    # Listed medium, high, low in agy's output, but the slider runs low, medium, high.
    ("Gemini 4 Argon (Medium)", {"search": "Gemini 4 Argon", "levels": 3, "effort_index": 1}),
    # One row only: the picker shows no slider for it.
    ("GPT-OSS 120B (Medium)", {"search": "GPT-OSS 120B", "levels": 1, "effort_index": 0}),
    ("Some New Model", {"search": "Some New Model", "levels": 1, "effort_index": 0}),
])
def test_plan_searches_the_model_name_and_places_the_effort_slider(label: str, expected: dict) -> None:
    assert agy_models.swap_plan(label, ROWS) == expected


def test_models_are_grouped_as_the_picker_shows_them_with_efforts_lowest_first() -> None:
    assert agy_models.model_choices(ROWS) == [
        {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "efforts": ["low", "medium", "high"]},
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "efforts": ["low", "medium", "high"]},
        {"id": "gpt-oss-120b", "label": "GPT-OSS 120B", "efforts": ["medium"]},
    ]


@pytest.mark.parametrize("model, effort, expected", [
    ("claude-opus-5-5", "high", "claude-opus-5-5-high"),
    ("claude-opus-5-5", "LOW", "claude-opus-5-5-low"),
    ("gpt-oss-120b", "medium", "gpt-oss-120b-medium"),
    # No effort: a full row id (as an earlier send stored it) is that row, a model alone is its medium row.
    ("claude-opus-5-5-high", "", "claude-opus-5-5-high"),
    ("gemini-4-argon", "", "gemini-4-argon-medium"),
    ("gpt-oss-120b", "", "gpt-oss-120b-medium"),
    # An effort the model does not have, or a model Agy no longer lists, is not guessed at.
    ("gpt-oss-120b", "high", None),
    ("retired-model", "", None),
])
def test_a_chosen_model_and_effort_is_looked_up_as_one_agy_row(model: str, effort: str, expected: str | None) -> None:
    row = agy_models.find_row(ROWS, model, effort)
    assert (row["id"] if row else None) == expected


# A stand-in for tmux that behaves like Agy's picker as seen on an example destination (agy 1.4.x): `/model` opens it,
# typing searches, Left/Right move an effort slider that stops at its ends, Enter prints "Model set to".
FAKE_TMUX = textwrap.dedent('''
    #!{python}
    import json, os, sys
    state_path = os.environ["FAKE_TMUX_STATE"]
    state = json.load(open(state_path))
    MODELS = {{"Gemini 4 Argon": ["Low", "Medium", "High"], "Claude Opus 5.5": ["Low", "Medium", "High"], "GPT-OSS 120B": ["Medium"]}}
    args = sys.argv[1:]
    command = args[0]
    state["calls"].append(args)
    if command == "capture-pane":
        if state["mode"] == "picker":
            print("Switch Model\\n\\n  Search: " + state["search"] + "\\n\\n> (list)\\n\\n  Effort slider")
        else:
            print("header\\n  " + state["note"] + "\\n>")
    elif command == "send-keys":
        literal = "-l" in args
        key = args[-1]
        if literal and state["mode"] == "prompt":
            state["typed"] += key
        elif literal and state["mode"] == "picker":
            state["search"] += key
        elif key == "Enter" and state["mode"] == "prompt" and state["typed"] == "/model" and not state["broken"]:
            state["mode"], state["search"], state["effort"], state["typed"] = "picker", "", 1, ""
        elif key == "Left" and state["mode"] == "picker":
            state["effort"] = max(0, state["effort"] - 1)
        elif key == "Right" and state["mode"] == "picker":
            state["effort"] = min(len(MODELS[next(m for m in MODELS if state["search"] in m)]) - 1, state["effort"] + 1)
        elif key == "Enter" and state["mode"] == "picker":
            base = next(m for m in MODELS if state["search"] in m)
            levels = MODELS[base]
            tier = levels[state["effort"]] if len(levels) > 1 else levels[0]
            state["note"] = "Model set to " + base + " (" + tier + ")"
            state["mode"] = "prompt"
        elif key == "Escape":
            state["mode"] = "prompt"
    json.dump(state, open(state_path, "w"))
''')


@pytest.fixture
def fake_tmux(tmp_path: Path, monkeypatch):
    program = tmp_path / "bin" / "tmux"
    program.parent.mkdir()
    program.write_text(FAKE_TMUX.lstrip().format(python=sys.executable))
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    state_path = tmp_path / "tmux_state.json"
    state_path.write_text(json.dumps({"mode": "prompt", "typed": "", "search": "", "effort": 1, "note": "", "broken": False, "calls": []}))
    monkeypatch.setenv("PATH", f"{program.parent}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_TMUX_STATE", str(state_path))
    monkeypatch.setattr(agy_remote.time, "sleep", lambda seconds: None)
    return state_path


def swap(label: str) -> dict:
    return agy_remote.swap({"name": "Chat-one", "label": label, **agy_models.swap_plan(label, ROWS)})


@pytest.mark.parametrize("label", ["Claude Opus 5.5 (High)", "Claude Opus 5.5 (Low)", "Gemini 4 Argon (Medium)", "GPT-OSS 120B (Medium)"])
def test_terminal_swap_types_the_search_moves_the_slider_and_sees_the_confirmation(fake_tmux: Path, label: str) -> None:
    assert swap(label) == {"ok": True, "detail": f"Model set to {label}"}
    calls = json.loads(fake_tmux.read_text())["calls"]
    # Text goes in literally (no key-name interpretation) and every command targets the chat's own session.
    assert ["send-keys", "-t", "=minds-Chat-one:", "-l", "/model"] in calls
    assert all(call[call.index("-t") + 1] == "=minds-Chat-one:" for call in calls)


def test_terminal_swap_reports_a_picker_that_never_opens(fake_tmux: Path, monkeypatch) -> None:
    state = json.loads(fake_tmux.read_text())
    state["broken"] = True
    fake_tmux.write_text(json.dumps(state))
    monkeypatch.setattr(agy_remote, "PICKER_WAIT_SECONDS", 0.05)
    answer = swap("Claude Opus 5.5 (High)")
    assert answer["ok"] is False and "picker did not open" in answer["detail"]
    # It leaves the terminal at its prompt rather than half inside a menu.
    assert json.loads(fake_tmux.read_text())["mode"] == "prompt"


def test_terminal_swap_reports_a_different_model_than_asked(fake_tmux: Path) -> None:
    answer = agy_remote.swap({"name": "Chat-one", "label": "Claude Opus 5.5 (High)", "search": "Claude Opus 5.5", "levels": 3, "effort_index": 0})
    assert answer["ok"] is False and answer["detail"] == "Agy set a different model"


def test_models_request_rejects_an_account_that_could_walk_out_of_the_accounts_folder() -> None:
    assert agy_remote.models({"account_id": "../../etc"}) == {"ok": False, "detail": "Invalid account"}


# ---- the note's delivery -------------------------------------------------------------------------------


class Machine:
    """A linked machine: its chat service and its Agy terminal, as the notes app sees them."""

    def __init__(self, swap_works: bool = True, create_status: int = 201, already_on_model: str = "",
                 split_effort: bool = False) -> None:
        self.swap_works, self.create_status = swap_works, create_status
        self.action_id = ""
        self.model = already_on_model
        # Newer Agy (1.4.12 on an example destination): the chat app reports "Gemini 3.8 Flash" and effort "high" apart.
        self.split_effort = split_effort
        self.requests: list[tuple[str, str, Any]] = []
        self.swaps: list[dict] = []

    def chat_request(self, machine, method, path, payload=None, timeout=45):
        self.requests.append((method, path, payload))
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "agy-account", "harness": "antigravity", "provider": "Google"}]}
        if path == "/api/chats/create":
            if self.create_status != 201:
                return self.create_status, {"detail": "still creating"}
            return 201, {"chat_id": "chat-1", "name": "Agy-note", "display_name": "Agy note"}
        if path == "/api/chats":
            identity = {"model_id": self.model, "effort": None}
            tiered = agy_models.TIERED_LABEL.match(self.model)
            if self.split_effort and tiered:
                identity = {"model_id": tiered["base"], "effort": tiered["tier"].lower()}
            chat = {"chat_id": "chat-1", "name": "Agy-note", "labels": {runner.ACTION_LABEL: self.action_id},
                    "active_agent": {"state": "RUNNING", "model_choice": {"identity": identity}}}
            return 200, {"chats": [chat]}
        if path.endswith("/destroy") or path.endswith("/message"):
            return 200, {"status": "ok"}
        raise AssertionError(path)

    def agy_remote(self, machine, request, timeout):
        if request["op"] == "models":
            return {"ok": True, "text": AGY_MODELS_OUTPUT}
        self.swaps.append(request)
        if self.swap_works:
            self.model = request["label"]
        return {"ok": self.swap_works, "detail": "Model set to " + request["label"] if self.swap_works else "Agy's model picker did not open"}


def fire(client, monkeypatch, machine: Machine, model: str, effort: str = "") -> dict:
    monkeypatch.setenv("MNGR_AGENT_ID", "local-machine")
    launched = []

    class WaitingThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            launched.append(self)

    monkeypatch.setattr(runner.threading, "Thread", WaitingThread)
    draft = client.post("/api/drafts", json={"title": "Agy note", "body": "Send to Agy"}).get_json()
    action = client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "new", "machine": "fresh", "machine_name": "an example destination", "provider": "agy", "model": model,
        "model_label": "Claude Opus 5.5", "effort": effort, "chat_name": "Agy note"}}).get_json()
    machine.action_id = action["id"]
    monkeypatch.setattr(runner, "chat_request", machine.chat_request)
    monkeypatch.setattr(runner, "agy_remote", machine.agy_remote)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    launched[0].target(*launched[0].args)
    return action


def state_of(client, action: dict) -> dict:
    sent = {item["id"]: item for item in client.get("/api/fired").get_json()["actions"]}
    return sent.get(action["id"], {"state": "landed"})


def test_a_chosen_agy_model_is_set_and_checked_before_the_note_is_sent(monkeypatch) -> None:
    machine = Machine()
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "claude-opus-5-5", "high")
    assert state_of(client, action)["state"] == "landed"
    create = next(payload for method, path, payload in machine.requests if path == "/api/chats/create")
    # Created empty and without a model pick the chat app would drop; the note travels afterwards.
    assert "message" not in create and "model" not in create and create["account_id"] == "agy-account"
    assert machine.swaps == [{"op": "swap", "name": "Agy-note", "label": "Claude Opus 5.5 (High)", "search": "Claude Opus 5.5", "levels": 3, "effort_index": 2}]
    paths = [path for method, path, payload in machine.requests]
    assert paths.index("/api/chats/chat-1/message") > max(i for i, path in enumerate(paths) if path == "/api/chats")
    message = next(payload for method, path, payload in machine.requests if path.endswith("/message"))
    assert message == {"message": "Send to Agy", "message_id": action["id"]}


def test_a_switch_is_confirmed_when_the_chat_app_reports_model_and_effort_apart(monkeypatch) -> None:
    machine = Machine(split_effort=True)
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "claude-opus-5-5", "high")
    assert state_of(client, action)["state"] == "landed"
    assert [swap["label"] for swap in machine.swaps] == ["Claude Opus 5.5 (High)"]


def test_a_reported_effort_other_than_the_chosen_one_is_not_taken_as_the_switch() -> None:
    assert not runner.same_model({"model_id": "Claude Opus 5.5", "effort": "low"}, "Claude Opus 5.5 (High)")
    assert runner.same_model({"model_id": "GPT-OSS 120B", "effort": ""}, "GPT-OSS 120B")


def test_a_send_stored_with_a_whole_row_id_and_no_effort_still_resolves(monkeypatch) -> None:
    machine = Machine()
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "claude-opus-5-5-high")
    assert state_of(client, action)["state"] == "landed"
    assert [swap["label"] for swap in machine.swaps] == ["Claude Opus 5.5 (High)"]


def test_a_chat_already_on_the_chosen_model_is_not_changed(monkeypatch) -> None:
    machine = Machine(already_on_model="Claude Opus 5.5 (High)")
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "claude-opus-5-5", "high")
    assert state_of(client, action)["state"] == "landed" and machine.swaps == []


def test_when_the_model_will_not_change_nothing_is_sent_and_the_empty_chat_is_removed(monkeypatch) -> None:
    machine = Machine(swap_works=False)
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "claude-opus-5-5", "high")
    sent = state_of(client, action)
    assert sent["state"] == "attention"
    assert "Agy would not switch to Claude Opus 5.5 (High)" in sent["error"] and "model picker did not open" in sent["error"]
    assert "The new empty chat was removed" in sent["error"]
    paths = [path for method, path, payload in machine.requests]
    assert "/api/chats/chat-1/destroy" in paths and "/api/chats/chat-1/message" not in paths
    # The note is still in the history to resend.
    assert sent["body"] == "Send to Agy"


def test_a_model_agy_no_longer_lists_is_refused_before_anything_is_created(monkeypatch) -> None:
    machine = Machine()
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "retired-model")
    assert state_of(client, action)["state"] == "attention"
    assert "does not offer that model and effort" in state_of(client, action)["error"]
    assert all(path != "/api/chats/create" for method, path, payload in machine.requests)


def test_a_slow_create_is_waited_for_then_carries_on(monkeypatch) -> None:
    machine = Machine(create_status=504)
    client = runner.app.test_client()
    action = fire(client, monkeypatch, machine, "claude-opus-5-5", "high")
    assert state_of(client, action)["state"] == "landed" and len(machine.swaps) == 1


def test_the_models_on_offer_are_the_default_then_what_agy_lists_on_that_machine(monkeypatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "local")

    def remote(machine, method, path, payload=None, timeout=45):
        if path == "/api/harnesses":
            return 200, {"antigravity": {"switch_mode": "read_only", "options": []}}
        assert path == "/api/accounts"
        return 200, {"accounts": [{"id": "agy-account", "harness": "antigravity", "provider": "Google"}]}

    answers = [{"ok": True, "text": AGY_MODELS_OUTPUT}]

    def models_on_machine(machine, request, timeout):
        assert request == {"op": "models", "account_id": "agy-account"}
        if isinstance(answers[0], Exception):
            raise answers[0]
        return answers[0]

    monkeypatch.setattr(runner, "chat_request", remote)
    monkeypatch.setattr(runner, "agy_remote", models_on_machine)
    client = runner.app.test_client()
    response = client.get("/api/chat-models/agy?machine=fresh&fresh=1").get_json()
    assert response["available"] is True
    assert response["models"] == [
        {"id": runner.PROVIDER_DEFAULT_MODEL, "label": "Agy's own default model", "efforts": []},
        *agy_models.model_choices(ROWS),
    ]
    assert response["models"][2] == {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "efforts": ["low", "medium", "high"]}

    # If the machine cannot say what Agy lists, the default is still there to send to.
    answers[0] = OSError("Selected machine did not respond")
    response = client.get("/api/chat-models/agy?machine=fresh&fresh=1").get_json()
    assert [model["id"] for model in response["models"]] == [runner.PROVIDER_DEFAULT_MODEL]
