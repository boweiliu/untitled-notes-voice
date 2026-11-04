"""Runs ON the machine that hosts an Agy chat. Standard library only.

The notes app sends this file's text to ``python3 -c`` (over ssh for a linked machine, or
locally) with one JSON request on stdin, and reads one JSON answer from stdout. Two requests:

``{"op": "models", "account_id": ...}``
    ``agy models`` for that account (the live list, one row per model and effort).
``{"op": "swap", "name": ..., "label": ..., "search": ..., "levels": ..., "effort_index": ...}``
    Type ``/model`` into the chat's terminal session, search for ``search``, move the effort
    slider to ``effort_index`` of ``levels``, press Enter, and report what the terminal said.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROMPT_WAIT_SECONDS = 90
PICKER_WAIT_SECONDS = 20


def models(request: dict) -> dict:
    account_id = str(request.get("account_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
        return {"ok": False, "detail": "Invalid account"}
    roots = [os.environ.get("MINDS_ACCOUNTS_ROOT", ""), "/home/user/.minds/accounts", str(Path.home() / ".minds" / "accounts")]
    home = next((Path(root) / account_id for root in roots if root and (Path(root) / account_id).is_dir()), None)
    if home is None:
        return {"ok": False, "detail": "Agy account folder not found"}
    result = subprocess.run(
        ["agy", "models"], env={**os.environ, "HOME": str(home)}, cwd="/tmp",
        capture_output=True, text=True, timeout=60, check=False,
    )
    return {"ok": result.returncode == 0, "text": result.stdout, "detail": result.stderr[-300:]}


def swap(request: dict) -> dict:
    target = f"=minds-{request['name']}:"

    def tmux(*args: str) -> str:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=15, check=False).stdout

    def screen() -> str:
        return tmux("capture-pane", "-t", target, "-p")

    def wait_for(test, seconds: float) -> str | None:
        deadline = time.time() + seconds
        while True:
            text = screen()
            if test(text):
                return text
            if time.time() > deadline:
                return None
            time.sleep(1)

    def type_text(text: str) -> None:
        tmux("send-keys", "-t", target, "-l", text)
        time.sleep(0.4)

    def press(key: str) -> None:
        tmux("send-keys", "-t", target, key)
        time.sleep(0.4)

    def leave(detail: str, text: str) -> dict:
        press("Escape")
        press("Escape")
        return {"ok": False, "detail": detail, "screen": text[-1200:]}

    if wait_for(lambda text: any(line.startswith(">") for line in text.splitlines()), PROMPT_WAIT_SECONDS) is None:
        return {"ok": False, "detail": "Agy did not show its prompt", "screen": screen()[-1200:]}
    type_text("/model")
    press("Enter")
    opened = wait_for(lambda text: "Switch Model" in text, PICKER_WAIT_SECONDS)
    if opened is None:
        return leave("Agy's model picker did not open", screen())
    type_text(request["search"])
    searched = wait_for(lambda text: f"Search: {request['search']}" in text, PICKER_WAIT_SECONDS)
    if searched is None:
        return leave("Could not search Agy's model picker", screen())
    if int(request["levels"]) > 1:
        # Left and Right stop at the ends of the slider: go to the far left, then step right.
        for _ in range(int(request["levels"])):
            press("Left")
        for _ in range(int(request["effort_index"])):
            press("Right")
    press("Enter")
    expected = f"Model set to {request['label']}"
    done = wait_for(lambda text: "Model set to" in text, PICKER_WAIT_SECONDS)
    if done is None:
        return leave("Agy did not confirm a model change", screen())
    if expected not in done:
        return {"ok": False, "detail": "Agy set a different model", "screen": done[-1200:]}
    return {"ok": True, "detail": expected}


def main() -> None:
    request = json.load(sys.stdin)
    handlers = {"models": models, "swap": swap}
    try:
        answer = handlers[request["op"]](request)
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        answer = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
