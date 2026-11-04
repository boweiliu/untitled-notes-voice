"""pi-dev's subscriptions: the choices offered, the machine helper, and the note's delivery to a chat on one."""
from typing import Any

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from prompt_notes import pi_remote, pi_subscriptions, runner

CLAUDE_ACCOUNT = {"id": "claude-acct", "harness": "pi-coding", "label": "Claude subscription (Pi)"}
ROUTER_ACCOUNT = {"id": "router-acct", "harness": "pi-coding", "label": "OpenRouter (Pi)"}
ADAPTIVE = {"off": None, "minimal": None, "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}


CLAUDE_ANSWER = {
    "account_id": "claude-acct", "ok": True, "providers": ["claude-code"],
    "models": [
        {"provider": "claude-code", "id": "default", "name": "Default (Claude Opus 5.5)", "reasoning": True, "thinkingLevelMap": ADAPTIVE},
        {"provider": "claude-code", "id": "claude-opus-5-5", "name": "Claude Opus 5.5", "reasoning": True, "thinkingLevelMap": ADAPTIVE},
        {"provider": "claude-code", "id": "claude-sonnet-5-5", "name": "Sonnet (Claude Code)", "reasoning": True,
         "thinkingLevelMap": {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}},
        {"provider": "claude-code", "id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "reasoning": True, "thinkingLevelMap": {}},
        # Signed in through the environment rather than this account's file: not this subscription's.
        {"provider": "openrouter", "id": "z-ai/glm-5.3", "name": "GLM 5.3", "reasoning": False, "thinkingLevelMap": {}},
    ],
}


# ---- the choices --------------------------------------------------------------------------------------


@pytest.mark.parametrize("model, expected", [
    ({"reasoning": False, "thinkingLevelMap": {"high": "high"}}, ["off"]),
    ({"reasoning": True, "thinkingLevelMap": {}}, ["off", "minimal", "low", "medium", "high"]),
    ({"reasoning": True, "thinkingLevelMap": ADAPTIVE}, ["low", "medium", "high", "xhigh", "max"]),
    ({"reasoning": True, "thinkingLevelMap": {"off": None, "xhigh": "xhigh"}}, ["minimal", "low", "medium", "high", "xhigh"]),
])
def test_effort_levels_follow_pis_own_rule(model: dict, expected: list[str]) -> None:
    assert pi_subscriptions.supported_thinking_levels(model) == expected


@pytest.mark.parametrize("provider, model_id, name, expected", [
    ("claude-code", "claude-opus-5-5", "Claude Opus 5.5", "Opus 5.5"),
    ("claude-code", "claude-sonnet-5-5", "Sonnet (Claude Code)", "Sonnet 5.5"),
    ("claude-code", "claude-haiku-4-5-20251001", "Claude Haiku 4.5", "Haiku 4.5"),
    ("claude-code", "claude-sonnet-5", "Sonnet 5", "Sonnet 5"),
    ("meridian", "claude-opus-5-5", "Claude Opus 5.5 (Meridian)", "Opus 5.5"),
    ("openai-codex", "gpt-6-sol", "GPT-6 Sol", "GPT-6 Sol"),
    ("opencode", "big-pickle", "", "big-pickle"),
    ("openrouter", "anthropic/claude-opus-5.5", "Anthropic: Claude Opus 5.5", "Claude Opus 5.5"),
])
def test_models_are_named_as_people_say_them(provider: str, model_id: str, name: str, expected: str) -> None:
    assert pi_subscriptions.model_label(provider, model_id, name) == expected


def test_the_three_subscriptions_come_first_in_order_and_unset_ones_say_so() -> None:
    router_answer = {"account_id": "router-acct", "ok": True, "providers": ["openrouter"], "models": [
        {"provider": "openrouter", "id": "z-ai/glm-5.3", "name": "GLM 5.3", "reasoning": False}]}
    choices = pi_subscriptions.build_choices(
        [ROUTER_ACCOUNT, CLAUDE_ACCOUNT], {"claude-acct": CLAUDE_ANSWER, "router-acct": router_answer})
    assert [(item["id"], item["label"], item["account_id"], item["available"]) for item in choices["subscriptions"]] == [
        ("opencode", "OpenCode", "", False),
        ("claude", "Claude", "claude-acct", True),
        ("codex", "Codex (ChatGPT)", "", False),
        ("account:router-acct", "OpenRouter", "router-acct", True),
    ]
    assert choices["subscriptions"][0]["detail"] == pi_subscriptions.NOT_CONNECTED
    claude_models = [model for model in choices["models"] if model["subscription"] == "claude"]
    # The add-on's "default" alias is left out, and a provider the account does not hold is not its model.
    assert [(model["id"], model["label"]) for model in claude_models] == [
        ("claude-code/claude-opus-5-5", "Opus 5.5"),
        ("claude-code/claude-sonnet-5-5", "Sonnet 5.5"),
        ("claude-code/claude-haiku-4-5-20251001", "Haiku 4.5"),
    ]
    assert claude_models[0]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert [model["id"] for model in choices["models"] if model["subscription"] == "account:router-acct"] == ["openrouter/z-ai/glm-5.3"]


def test_openrouter_offers_only_the_chosen_coding_models_in_their_order() -> None:
    router_answer = {"account_id": "router-acct", "ok": True, "providers": ["openrouter"], "models": [
        {"provider": "openrouter", "id": model_id, "name": name, "reasoning": False}
        for model_id, name in [
            ("openai/gpt-3.5-turbo", "OpenAI: GPT-3.5 Turbo"),
            ("minimax/minimax-m3", "MiniMax: MiniMax M3"),
            ("anthropic/claude-opus-5.5", "Anthropic: Claude Opus 5.5"),
            ("amazon/nova-micro-v1", "Amazon: Nova Micro 1.0"),
        ]
    ]}
    choices = pi_subscriptions.build_choices([ROUTER_ACCOUNT], {"router-acct": router_answer})
    assert [(model["id"], model["label"]) for model in choices["models"]] == [
        ("openrouter/anthropic/claude-opus-5.5", "Claude Opus 5.5"),
        ("openrouter/minimax/minimax-m3", "MiniMax M3"),
    ]


def test_an_account_the_machine_could_not_answer_for_is_shown_but_not_offered() -> None:
    choices = pi_subscriptions.build_choices([CLAUDE_ACCOUNT], {"claude-acct": {"ok": False, "detail": "Account folder not found"}})
    claude = next(item for item in choices["subscriptions"] if item["id"] == "claude-acct" or item["account_id"] == "claude-acct")
    # With no providers known it cannot be recognised as Claude, so it keeps its own name.
    assert (claude["label"], claude["available"], claude["detail"]) == ("Claude subscription", False, "Account folder not found")
    assert choices["models"] == []


def test_a_second_account_of_the_same_kind_is_offered_under_its_own_name() -> None:
    second = {"id": "claude-2", "harness": "pi-coding", "label": "Claude subscription 2 (Pi)"}
    answer = {**CLAUDE_ANSWER, "account_id": "claude-2"}
    choices = pi_subscriptions.build_choices([CLAUDE_ACCOUNT, second], {"claude-acct": CLAUDE_ANSWER, "claude-2": answer})
    assert [item["id"] for item in choices["subscriptions"]] == ["opencode", "claude", "codex", "account:claude-2"]


# ---- the helper that runs on the machine -------------------------------------------------------------


# A stand-in for `pi --mode rpc`: answers get_available_models first with the add-on's built-in list,
# then (as the real add-on does once `claude` has answered) with the list it discovered.
FAKE_PI = textwrap.dedent('''
    #!{python}
    import json, os, sys, time
    started = time.time()
    agent_dir = os.environ["PI_CODING_AGENT_DIR"]
    with open(os.environ["FAKE_PI_SEEN"], "a") as seen:
        seen.write(json.dumps({{"auth": os.path.islink(os.path.join(agent_dir, "auth.json")),
                               "extensions": os.path.islink(os.path.join(agent_dir, "extensions"))}}) + "\\n")
    BUILT_IN = [{{"provider": "claude-code", "id": "claude-sonnet-5", "name": "Sonnet 5", "reasoning": True}}]
    DISCOVERED = [{{"provider": "claude-code", "id": "claude-sonnet-5-5", "name": "Sonnet (Claude Code)", "reasoning": True}}]
    for line in sys.stdin:
        models = DISCOVERED if time.time() - started > 1.5 else BUILT_IN
        print(json.dumps({{"type": "response", "command": "get_available_models", "success": True, "data": {{"models": models}}}}), flush=True)
''')


@pytest.fixture()
def fake_machine(tmp_path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pi = bin_dir / "pi"
    pi.write_text(FAKE_PI.format(python=sys.executable).lstrip())
    pi.chmod(pi.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PI_SEEN", str(tmp_path / "seen.jsonl"))
    accounts = tmp_path / "accounts"
    (accounts / "claude-acct").mkdir(parents=True)
    (accounts / "claude-acct" / "auth.json").write_text(json.dumps({"claude-code": {"type": "api_key", "key": "x", "env": {}}}))
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(accounts))
    home = tmp_path / "home"
    (home / ".pi" / "agent" / "extensions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pi_remote, "MODELS_MIN_SECONDS", 2)
    monkeypatch.setattr(pi_remote, "MODELS_SETTLE_SECONDS", 1.5)
    monkeypatch.setattr(pi_remote, "MODELS_WAIT_SECONDS", 10)
    return tmp_path


def test_subscriptions_wait_for_the_discovered_models_and_name_the_providers(fake_machine: Path) -> None:
    answer = pi_remote.subscriptions({"account_ids": ["claude-acct", "../escape", "missing"]})
    by_id = {item["account_id"]: item for item in answer["accounts"]}
    assert by_id["claude-acct"]["providers"] == ["claude-code"]
    assert [model["id"] for model in by_id["claude-acct"]["models"]] == ["claude-sonnet-5-5"]
    assert by_id["../escape"]["ok"] is False and by_id["missing"]["detail"] == "Account folder not found"
    # pi ran on a scratch folder holding the account's credential and this machine's extensions.
    seen = [json.loads(line) for line in (fake_machine / "seen.jsonl").read_text().splitlines()]
    assert seen == [{"auth": True, "extensions": True}]


def test_apply_writes_pis_model_mailbox_and_waits_for_pi_to_report_it(tmp_path, monkeypatch) -> None:
    agents = tmp_path / "agents"
    folder = agents / "agent-1"
    folder.mkdir(parents=True)
    monkeypatch.setenv("MNGR_AGENTS_DIR", str(agents))

    def pi_applies_it() -> None:
        while not (folder / "pi_control.json").exists():
            time.sleep(0.05)
        intent = json.loads((folder / "pi_control.json").read_text())
        (folder / "pi_control.json").unlink()
        (folder / "model_state.json").write_text(json.dumps({"model": intent["model_id"], "effort": intent["thinking_level"], "fast": False}))

    threading.Thread(target=pi_applies_it, daemon=True).start()
    answer = pi_remote.apply({"chat_id": "agent-1", "model_id": "claude-code/claude-opus-5-5", "thinking_level": "high"})
    assert answer == {"ok": True, "detail": "Model set to claude-code/claude-opus-5-5 (high)"}


def test_apply_sends_the_choice_again_when_switching_model_moved_the_effort(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "agents" / "agent-1"
    folder.mkdir(parents=True)
    monkeypatch.setenv("MNGR_AGENTS_DIR", str(tmp_path / "agents"))
    applied = []

    def pi_like_its_lifecycle_hook() -> None:
        # Effort first, then the model; the switch moves an effort the old model lacked to the new model's nearest.
        current = {"model": "claude-code/default", "effort": "off"}
        while len(applied) < 2:
            if (folder / "pi_control.json").exists():
                intent = json.loads((folder / "pi_control.json").read_text())
                (folder / "pi_control.json").unlink()
                applied.append(intent)
                switching = current["model"] != intent["model_id"]
                current = {"model": intent["model_id"], "effort": "high" if switching else intent["thinking_level"]}
                (folder / "model_state.json").write_text(json.dumps(current))
            time.sleep(0.05)

    threading.Thread(target=pi_like_its_lifecycle_hook, daemon=True).start()
    answer = pi_remote.apply({"chat_id": "agent-1", "model_id": "opencode-go/glm-5.3", "thinking_level": "low"})
    assert answer == {"ok": True, "detail": "Model set to opencode-go/glm-5.3 (low)"}
    assert len(applied) == 2


def test_apply_reports_a_model_pi_never_took_up(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "agents" / "agent-1"
    folder.mkdir(parents=True)
    (folder / "model_state.json").write_text(json.dumps({"model": "claude-code/default", "effort": "medium"}))
    monkeypatch.setenv("MNGR_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(pi_remote, "APPLY_WAIT_SECONDS", 1)
    answer = pi_remote.apply({"chat_id": "agent-1", "model_id": "claude-code/claude-opus-5-5", "thinking_level": "high"})
    assert answer == {"ok": False, "detail": "pi still reports claude-code/default (medium)"}
    assert pi_remote.apply({"chat_id": "../agent-1", "model_id": "x"})["ok"] is False


# ---- the note's delivery -------------------------------------------------------------------------------


class Machine:
    """A machine with a Claude subscription and an OpenRouter pi account, as the notes app sees it."""

    def __init__(self, applies: bool = True) -> None:
        self.applies = applies
        self.requests: list[tuple[str, str, Any]] = []
        self.applied: list[dict] = []

    def chat_request(self, machine, method, path, payload=None, timeout=45):
        self.requests.append((method, path, payload))
        if path == "/api/accounts":
            return 200, {"accounts": [ROUTER_ACCOUNT, CLAUDE_ACCOUNT]}
        if path == "/api/chats/create":
            return 201, {"chat_id": "agent-9", "name": "pi-note", "display_name": "pi note"}
        if path.endswith("/destroy") or path.endswith("/message"):
            return 200, {"status": "ok"}
        raise AssertionError(path)

    def pi_remote(self, machine, request, timeout):
        assert request["op"] == "apply"
        self.applied.append(request)
        return {"ok": self.applies, "detail": "Model set" if self.applies else "pi still reports claude-code/default"}


def fire(client, monkeypatch, machine: Machine, destination: dict) -> dict:
    monkeypatch.setenv("MNGR_AGENT_ID", "local-machine")
    launched = []

    class WaitingThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            launched.append(self)

    monkeypatch.setattr(runner.threading, "Thread", WaitingThread)
    draft = client.post("/api/drafts", json={"title": "pi note", "body": "Send to pi"}).get_json()
    action = client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "new", "machine": "talker", "machine_name": "primary-example", "provider": "pi-dev", "chat_name": "pi note",
        **destination}}).get_json()
    monkeypatch.setattr(runner, "chat_request", machine.chat_request)
    monkeypatch.setattr(runner, "pi_remote", machine.pi_remote)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    launched[0].target(*launched[0].args)
    sent = {item["id"]: item for item in client.get("/api/fired").get_json()["actions"]}
    return sent.get(action["id"], {"state": "landed"})


CLAUDE_DESTINATION = {"subscription": "claude", "subscription_label": "Claude", "account_id": "claude-acct",
                      "model": "claude-code/claude-opus-5-5", "model_label": "Opus 5.5", "effort": "high"}


def test_a_claude_subscription_chat_is_put_on_its_model_before_the_note_is_sent(monkeypatch) -> None:
    machine = Machine()
    sent = fire(runner.app.test_client(), monkeypatch, machine, CLAUDE_DESTINATION)
    assert sent["state"] == "landed"
    create = next(payload for method, path, payload in machine.requests if path == "/api/chats/create")
    # On the subscription's own account, created empty and with no model pick the chat app would refuse.
    assert create["account_id"] == "claude-acct" and "message" not in create and "model" not in create
    assert machine.applied == [{"op": "apply", "chat_id": "agent-9", "model_id": "claude-code/claude-opus-5-5", "thinking_level": "high"}]
    message = next(payload for method, path, payload in machine.requests if path.endswith("/message"))
    assert message["message"] == "Send to pi"


def test_every_pi_dev_model_is_set_and_checked_on_the_chosen_account_before_the_note(monkeypatch) -> None:
    # A model the chat app does know is still set on the running chat: its pick at create lost the effort
    # of a model without "medium" (GLM-5.3 asked for low came out high).
    machine = Machine()
    sent = fire(runner.app.test_client(), monkeypatch, machine, {
        "subscription": "account:router-acct", "account_id": "router-acct", "model": "openrouter/z-ai/glm-5.3", "effort": "low"})
    assert sent["state"] == "landed"
    create = next(payload for method, path, payload in machine.requests if path == "/api/chats/create")
    assert create["account_id"] == "router-acct" and "message" not in create and "model" not in create
    assert machine.applied == [{"op": "apply", "chat_id": "agent-9", "model_id": "openrouter/z-ai/glm-5.3", "thinking_level": "low"}]


def test_when_pi_will_not_take_the_model_nothing_is_sent_and_the_empty_chat_is_removed(monkeypatch) -> None:
    machine = Machine(applies=False)
    sent = fire(runner.app.test_client(), monkeypatch, machine, CLAUDE_DESTINATION)
    assert sent["state"] == "attention"
    assert "pi would not switch to Opus 5.5" in sent["error"] and "The new empty chat was removed" in sent["error"]
    paths = [path for method, path, payload in machine.requests]
    assert "/api/chats/agent-9/destroy" in paths and not any(path.endswith("/message") for path in paths)


def test_a_subscription_since_removed_from_the_machine_is_not_swapped_for_another(monkeypatch) -> None:
    machine = Machine()
    sent = fire(runner.app.test_client(), monkeypatch, machine, {**CLAUDE_DESTINATION, "account_id": "gone"})
    assert sent["state"] == "attention" and "no longer set up" in sent["error"]
    assert all(path != "/api/chats/create" for method, path, payload in machine.requests)


def test_pi_dev_choices_come_from_the_machine_per_subscription(monkeypatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "local")

    def remote(machine, method, path, payload=None, timeout=45):
        if path == "/api/harnesses":
            return 200, {"pi-coding": {"options": [{"id": "openrouter/z-ai/glm-5.3", "label": "x", "in_picker": True, "efforts": []}]}}
        return 200, {"accounts": [CLAUDE_ACCOUNT, {"id": "codex-acct", "harness": "codex"}]}

    asked = []

    def helper(machine, request, timeout):
        asked.append(request)
        return {"ok": True, "accounts": [CLAUDE_ANSWER]}

    monkeypatch.setattr(runner, "chat_request", remote)
    monkeypatch.setattr(runner, "pi_remote", helper)
    data = runner.app.test_client().get("/api/chat-models/pi-dev?machine=talker").get_json()
    # Only pi accounts are asked about.
    assert asked == [{"op": "subscriptions", "account_ids": ["claude-acct"]}]
    assert data["available"] is True
    assert [item["id"] for item in data["subscriptions"]] == ["opencode", "claude", "codex"]
    assert {model["subscription"] for model in data["models"]} == {"claude"}


def test_pi_dev_remembers_its_subscription_and_the_model_used_on_each() -> None:
    client = runner.app.test_client()
    defaults = {"provider": "pi-dev", "by_provider": {"pi-dev": {
        "model": "claude-code/claude-opus-5-5", "effort_by_model": {"claude-code/claude-opus-5-5": "high"},
        "subscription": "claude", "model_by_subscription": {"claude": "claude-code/claude-opus-5-5"}}}}
    assert client.put("/api/destination-defaults", json=defaults).status_code == 200
    assert client.get("/api/destination-defaults").get_json()["by_provider"]["pi-dev"]["subscription"] == "claude"
    bad = {"provider": "pi-dev", "by_provider": {"pi-dev": {"model": "", "model_by_subscription": {"claude": 5}}}}
    assert client.put("/api/destination-defaults", json=bad).status_code == 400


def test_an_opencode_account_takes_the_opencode_place_named_for_its_plan() -> None:
    go_account = {"id": "go-acct", "harness": "pi-coding", "label": "Opencode Go (Pi)"}
    go_answer = {"account_id": "go-acct", "ok": True, "providers": ["opencode-go"], "models": [
        {"provider": "opencode-go", "id": "kimi-k3", "name": "Kimi K3", "reasoning": True, "thinkingLevelMap": {}}]}
    choices = pi_subscriptions.build_choices([CLAUDE_ACCOUNT, go_account], {"claude-acct": CLAUDE_ANSWER, "go-acct": go_answer})
    first = choices["subscriptions"][0]
    assert (first["id"], first["label"], first["account_id"], first["available"]) == ("opencode", "OpenCode Go", "go-acct", True)
    assert [model["id"] for model in choices["models"] if model["subscription"] == "opencode"] == ["opencode-go/kimi-k3"]


def test_claude_models_come_in_claude_codes_picker_order() -> None:
    # pi lists them as Claude Code reports them (Opus, Fable, Sonnet, Haiku); the picker shows
    # Fable, Opus, Sonnet, Haiku, newest first within a family.
    names = [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-fable-5-1", "Claude Fable 5.1"),
             ("claude-sonnet-5-5", "Sonnet (Claude Code)"), ("claude-haiku-5-5", "Haiku (Claude Code)"),
             ("claude-opus-4-8", "Claude Opus 4.8"), ("claude-sonnet-5", "Sonnet 5")]
    answer = {"account_id": "claude-acct", "ok": True, "providers": ["claude-code"], "models": [
        {"provider": "claude-code", "id": model_id, "name": name, "reasoning": True, "thinkingLevelMap": {}}
        for model_id, name in names]}
    choices = pi_subscriptions.build_choices([CLAUDE_ACCOUNT], {"claude-acct": answer})
    assert [model["label"] for model in choices["models"]] == [
        "Fable 5.1", "Opus 5.5", "Opus 4.8", "Sonnet 5.5", "Sonnet 5", "Haiku 5.5"]


def test_the_shared_bridge_account_is_offered_under_its_own_name_in_claude_order() -> None:
    shared = {"id": "shared-acct", "harness": "pi-coding", "label": "Claude subscription (shared) (Pi)"}
    names = [("claude-sonnet-5-5", "Claude Sonnet 5.5 (Meridian)"), ("claude-opus-5-5", "Claude Opus 5.5 (Meridian)"),
             ("claude-fable-5-1", "Claude Fable 5.1 (Meridian)"), ("claude-haiku-5-5", "Claude Haiku 5.5 (Meridian)")]
    answer = {"account_id": "shared-acct", "ok": True, "providers": ["meridian"], "models": [
        {"provider": "meridian", "id": model_id, "name": name, "reasoning": True, "thinkingLevelMap": {}}
        for model_id, name in names]}
    choices = pi_subscriptions.build_choices([CLAUDE_ACCOUNT, shared], {"claude-acct": CLAUDE_ANSWER, "shared-acct": answer})
    assert [(item["id"], item["label"]) for item in choices["subscriptions"]] == [
        ("opencode", "OpenCode"), ("claude", "Claude"), ("codex", "Codex (ChatGPT)"),
        ("account:shared-acct", "Claude subscription (shared)")]
    assert [(model["id"], model["label"]) for model in choices["models"] if model["subscription"] == "account:shared-acct"] == [
        ("meridian/claude-fable-5-1", "Fable 5.1"), ("meridian/claude-opus-5-5", "Opus 5.5"),
        ("meridian/claude-sonnet-5-5", "Sonnet 5.5"), ("meridian/claude-haiku-5-5", "Haiku 5.5")]


def test_a_bundled_providers_models_are_read_from_pis_data_without_starting_pi(fake_machine: Path) -> None:
    # The stand-in pi records every start; a bundled provider's account must not cause one.
    data_dir = fake_machine / "bin" / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "providers" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "openrouter.json").write_text(json.dumps({"openai-completions": {
        "z-ai/glm-5.3": {"name": "GLM 5.3", "reasoning": True, "thinkingLevelMap": {"xhigh": "xhigh"}}}}))
    router = fake_machine / "accounts" / "router-acct"
    router.mkdir()
    (router / "auth.json").write_text(json.dumps({"openrouter": {"type": "api_key", "key": "x"}}))
    answer = pi_remote.subscriptions({"account_ids": ["router-acct"]})
    assert answer["accounts"][0]["models"] == [{"provider": "openrouter", "id": "z-ai/glm-5.3", "name": "GLM 5.3",
                                                "reasoning": True, "thinkingLevelMap": {"xhigh": "xhigh"}}]
    assert not (fake_machine / "seen.jsonl").exists()


# ---- models OpenCode has retired ---------------------------------------------------------------------

GO_ACCOUNT = {"id": "go-acct", "harness": "pi-coding", "label": "Opencode Go (Pi)"}
RETIRED_DESTINATION = {"subscription": "opencode", "subscription_label": "OpenCode Go", "account_id": "go-acct",
                       "model": "opencode-go/minimax-m2.7", "model_label": "MiniMax-M2.7", "effort": "medium"}


def go_model(model_id: str, name: str) -> dict:
    return {"provider": "opencode-go", "id": model_id, "name": name, "reasoning": True, "thinkingLevelMap": {}}


def test_a_model_opencode_retired_is_left_out_of_the_subscriptions_models() -> None:
    # pi 0.87.1 still lists MiniMax-M2.7, which OpenCode Go answers with "410 ModelDeprecated".
    answer = {"account_id": "go-acct", "ok": True, "providers": ["opencode-go"],
              "models": [go_model("minimax-m2.7", "MiniMax-M2.7"), go_model("minimax-m3", "MiniMax-M3")]}
    choices = pi_subscriptions.build_choices([GO_ACCOUNT], {"go-acct": answer})
    assert [model["id"] for model in choices["models"]] == ["opencode-go/minimax-m3"]


def test_a_model_list_saved_before_the_retirement_no_longer_offers_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MNGR_AGENT_ID", "local")
    saved = {"provider": "pi-dev", "available": True, "subscriptions": [], "models": [
        {"id": "opencode-go/minimax-m2.7", "label": "MiniMax-M2.7", "efforts": [], "subscription": "opencode"},
        {"id": "opencode-go/minimax-m3", "label": "MiniMax-M3", "efforts": [], "subscription": "opencode"}]}
    runner.remember_choice(tmp_path, "models-v4:fresh:pi-dev", saved)
    client = runner.app.test_client()
    listed = client.get("/api/chat-models/pi-dev?machine=fresh").get_json()
    assert [model["id"] for model in listed["models"]] == ["opencode-go/minimax-m3"]
    snapshot = client.get("/api/choice-cache").get_json()["catalogs"]["fresh:pi-dev"]["data"]
    assert [model["id"] for model in snapshot["models"]] == ["opencode-go/minimax-m3"]


def test_a_send_on_a_retired_model_is_refused_with_the_replacement_named(monkeypatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "local-machine")
    client = runner.app.test_client()
    draft = client.post("/api/drafts", json={"title": "pi note", "body": "Send to pi"}).get_json()
    answer = client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "new", "machine": "fresh", "machine_name": "example-machine", "provider": "pi-dev", "chat_name": "pi note",
        **RETIRED_DESTINATION}})
    assert answer.status_code == 400
    assert "OpenCode has retired MiniMax-M2.7" in answer.get_data(as_text=True)
    assert "minimax-m3.1-flash" in answer.get_data(as_text=True)
    # The draft stays where it was, so it can go out on another model.
    assert any(item["id"] == draft["id"] for item in client.get("/api/drafts").get_json()["drafts"])


def test_a_resend_on_a_retired_model_makes_no_empty_chat(monkeypatch) -> None:
    machine = Machine()
    refusal, retired_yet = runner.retired_model_refusal, [False]
    monkeypatch.setattr(runner, "retired_model_refusal", lambda destination: refusal(destination) if retired_yet[0] else "")
    monkeypatch.setenv("MNGR_AGENT_ID", "local-machine")
    client = runner.app.test_client()
    # Fired while the model still looked fine (the refusal is off), delivered after it was retired.
    launched = []

    class WaitingThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            launched.append(self)

    monkeypatch.setattr(runner.threading, "Thread", WaitingThread)
    draft = client.post("/api/drafts", json={"title": "pi note", "body": "Send to pi"}).get_json()
    action = client.post("/api/fire", json={"draft_id": draft["id"], "destination": {
        "mode": "new", "machine": "fresh", "machine_name": "example-machine", "provider": "pi-dev", "chat_name": "pi note",
        **RETIRED_DESTINATION}}).get_json()
    retired_yet[0] = True
    accounts = {"accounts": [GO_ACCOUNT]}
    monkeypatch.setattr(runner, "chat_request", lambda *args, **kwargs: (200, accounts) if args[2] == "/api/accounts" else machine.chat_request(*args, **kwargs))
    monkeypatch.setattr(runner, "pi_remote", machine.pi_remote)
    launched[0].target(*launched[0].args)
    sent = {item["id"]: item for item in client.get("/api/fired").get_json()["actions"]}[action["id"]]
    assert sent["state"] == "attention" and "OpenCode has retired MiniMax-M2.7" in sent["error"]
    assert machine.requests == [] and machine.applied == []
