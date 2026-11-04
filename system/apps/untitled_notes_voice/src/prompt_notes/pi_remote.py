"""Runs ON the machine that hosts a pi-dev chat. Standard library only.

The notes app sends this file's text to ``python3 -c`` (over ssh for a linked machine, or
locally) with one JSON request on stdin, and reads one JSON answer from stdout. Two requests:

``{"op": "subscriptions", "account_ids": [...]}``
    For each pi account folder named, the providers its ``auth.json`` holds (their names only,
    never a credential) and the models pi offers on it. A provider pi bundles (OpenRouter,
    OpenCode, ChatGPT) is read from pi's own model data files, without starting pi. Only an
    account holding a provider an extension adds (the Claude Code add-on's ``claude-code/...``)
    starts a ``pi --mode rpc``, on a scratch pi folder holding that account's ``auth.json`` and this
    machine's pi extensions, and one such pi at a time: each is a few hundred MB.
``{"op": "apply", "chat_id": ..., "model_id": ..., "thinking_level": ...}``
    Put a running pi chat on a model the chat app cannot set itself: write pi's one-slot model
    mailbox (``pi_control.json``, the same file the chat app writes) in the chat's agent folder and
    wait for pi's ``model_state.json`` to report that model and level.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# pi answers get_available_models as soon as it starts, but an extension first answers with its
# built-in list and replaces it a few seconds later with what its own program reports (the Claude
# Code add-on asks `claude`), often with the same number of models. So the request is repeated
# until the answer has stayed the same for MODELS_SETTLE_SECONDS, and at least MODELS_MIN_SECONDS
# have passed, or MODELS_WAIT_SECONDS have.
MODELS_WAIT_SECONDS = 30
MODELS_MIN_SECONDS = 8
MODELS_SETTLE_SECONDS = 4
APPLY_WAIT_SECONDS = 45
APPLY_ATTEMPTS = 3
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
# One pi at a time: several at once (one per account) were stopped by the machine's memory guard.
PI_RUNS = threading.Semaphore(1)


def bundled_data_dir() -> Path | None:
    """pi-ai's bundled provider data (``<pi-ai>/dist/providers/data/*.json``), found from the ``pi`` binary."""
    executable = shutil.which("pi")
    current = Path(executable).resolve() if executable else None
    while current is not None and current != current.parent:
        candidate = current / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "providers" / "data"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return None


def bundled_models(data_dir: Path, provider: str) -> list[dict]:
    """A bundled provider's models, from its data file (``{api: {model_id: model}}``)."""
    try:
        data = json.loads((data_dir / f"{provider}.json").read_text())
    except (OSError, ValueError):
        return []
    return [
        {"provider": provider, "id": model_id, **model}
        for by_id in (data.values() if isinstance(data, dict) else [])
        if isinstance(by_id, dict)
        for model_id, model in by_id.items()
        if isinstance(model, dict)
    ]


def roots(env_name: str, *paths: str) -> list[Path]:
    return [Path(path) for path in (os.environ.get(env_name, ""), *paths) if path]


def account_folder(account_id: str) -> Path | None:
    for root in roots("MINDS_ACCOUNTS_ROOT", "/home/user/.minds/accounts", str(Path.home() / ".minds" / "accounts")):
        if (root / account_id).is_dir():
            return root / account_id
    return None


def agent_folder(agent_id: str) -> Path | None:
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    for root in roots("MNGR_AGENTS_DIR", f"{host_dir}/agents" if host_dir else "", "/home/user/.mngr/agents", str(Path.home() / ".mngr" / "agents")):
        if (root / agent_id).is_dir():
            return root / agent_id
    return None


def rpc_models(pi_dir: Path) -> list[dict]:
    """``get_available_models`` from a pi started on ``pi_dir``, once its extensions' answers have settled."""
    process = subprocess.Popen(
        ["pi", "--mode", "rpc", "--no-session"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        cwd=tempfile.gettempdir(), env={**os.environ, "PI_CODING_AGENT_DIR": str(pi_dir)},
    )
    assert process.stdin is not None and process.stdout is not None
    answers: list[list[dict]] = []
    lock = threading.Lock()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("command") == "get_available_models" and message.get("success"):
                with lock:
                    answers.append(list((message.get("data") or {}).get("models") or []))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    latest: list[dict] = []
    started = changed_at = time.time()
    try:
        while time.time() < started + MODELS_WAIT_SECONDS:
            process.stdin.write(json.dumps({"type": "get_available_models"}) + "\n")
            process.stdin.flush()
            time.sleep(1)
            with lock:
                answer = answers[-1] if answers else []
            if model_keys(answer) != model_keys(latest):
                latest, changed_at = answer, time.time()
            now = time.time()
            if latest and now - started >= MODELS_MIN_SECONDS and now - changed_at >= MODELS_SETTLE_SECONDS:
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        process.kill()
        process.wait(timeout=10)
    return latest


def model_keys(models: list[dict]) -> list[tuple]:
    return [(model.get("provider"), model.get("id"), json.dumps(model.get("thinkingLevelMap"), sort_keys=True)) for model in models]


def subscription(account_id: str, extensions: Path) -> dict:
    folder = account_folder(account_id) if SAFE_ID.fullmatch(account_id) else None
    if folder is None:
        return {"account_id": account_id, "ok": False, "detail": "Account folder not found"}
    try:
        auth = json.loads((folder / "auth.json").read_text())
    except (OSError, ValueError):
        auth = {}
    providers = sorted(auth) if isinstance(auth, dict) else []
    data_dir = bundled_data_dir()
    if data_dir is not None and providers and all((data_dir / f"{provider}.json").is_file() for provider in providers):
        found = [model for provider in providers for model in bundled_models(data_dir, provider)]
    else:
        found = pi_models(folder, extensions)
    models = [
        {
            "provider": model.get("provider"), "id": model.get("id"), "name": model.get("name"),
            "reasoning": bool(model.get("reasoning")), "thinkingLevelMap": model.get("thinkingLevelMap") or {},
        }
        for model in found
        if isinstance(model, dict) and isinstance(model.get("provider"), str) and isinstance(model.get("id"), str)
    ]
    return {"account_id": account_id, "ok": True, "providers": providers, "models": models}


def pi_models(folder: Path, extensions: Path) -> list[dict]:
    """What a pi started on the account's credential and this machine's extensions offers."""
    with PI_RUNS:
        scratch = Path(tempfile.mkdtemp(prefix="notes-pi-models-"))
        try:
            (scratch / "auth.json").symlink_to(folder / "auth.json")
            if extensions.is_dir():
                (scratch / "extensions").symlink_to(extensions)
            return rpc_models(scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def subscriptions(request: dict) -> dict:
    if shutil.which("pi") is None:
        return {"ok": False, "detail": "pi is not installed on this machine"}
    extensions = Path.home() / ".pi" / "agent" / "extensions"
    account_ids = [str(item) for item in request.get("account_ids", [])][:12]
    results: dict[str, dict] = {}
    threads = [
        threading.Thread(target=lambda item=item: results.__setitem__(item, subscription(item, extensions)), daemon=True)
        for item in account_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(MODELS_WAIT_SECONDS + 20)
    return {"ok": True, "accounts": [results.get(item, {"account_id": item, "ok": False, "detail": "No answer"}) for item in account_ids]}


def apply(request: dict) -> dict:
    chat_id = str(request.get("chat_id", ""))
    folder = agent_folder(chat_id) if SAFE_ID.fullmatch(chat_id) else None
    if folder is None:
        return {"ok": False, "detail": "The chat's agent folder was not found"}
    model_id = str(request["model_id"])
    level = request.get("thinking_level") or None
    deadline = time.time() + APPLY_WAIT_SECONDS
    state: dict = {}
    # pi's lifecycle hook sets the effort before it switches the model, and switching moves the effort
    # to one the new model has (GLM-5.3 asked for "low" from a default model ends on "high"). Once the
    # model is in place, the same intent again only changes the effort, which then holds.
    for _ in range(APPLY_ATTEMPTS):
        write_intent(folder, model_id, level)
        while time.time() < deadline:
            time.sleep(0.5)
            state = model_state(folder)
            if state.get("model") == model_id and (level is None or state.get("effort") == level):
                return {"ok": True, "detail": f"Model set to {model_id}" + (f" ({level})" if level else "")}
            if state.get("model") == model_id and not (folder / "pi_control.json").exists():
                break
    reported = f"{state.get('model') or 'no model'}" + (f" ({state['effort']})" if state.get("effort") else "")
    return {"ok": False, "detail": f"pi still reports {reported}"}


def write_intent(folder: Path, model_id: str, level: str | None) -> None:
    # Atomic, like the chat app's own write: pi renames the file away when it applies it.
    scratch = folder / "pi_control.json.notes-tmp"
    scratch.write_text(json.dumps({"model_id": model_id, "thinking_level": level}))
    os.replace(scratch, folder / "pi_control.json")


def model_state(folder: Path) -> dict:
    try:
        state = json.loads((folder / "model_state.json").read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def main() -> None:
    request = json.load(sys.stdin)
    handlers = {"subscriptions": subscriptions, "apply": apply}
    try:
        answer = handlers[request["op"]](request)
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        answer = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
