"""Private prompt drafting window.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/prompt-notes/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/prompt-notes/`` but
  honors the ``PROMPT_NOTES_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``PROMPT_NOTES_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://prompt-notes.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import os
import json
import sqlite3
import subprocess
import shlex
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/prompt-notes/`` but is overridable via the ``PROMPT_NOTES_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/prompt-notes/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("PROMPT_NOTES_DATA_DIR", "data/.apps/untitled-notes-voice"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``PROMPT_NOTES_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("PROMPT_NOTES_PORT", "8096"))
CHAT_BASE_URL = os.environ.get("PROMPT_NOTES_CHAT_URL", "http://127.0.0.1:8010")
STUDIO_WORKSPACES_PATH = "/minds-api-proxy/api/v1/workspaces"

PROVIDER_HARNESSES = {
    "claude": "claude",
    "codex": "codex",
    "agy": "antigravity",
    "pi-dev": "pi-coding",
}

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>"""

app = Flask("prompt_notes", static_folder=None)
ACTION_LABEL = "untitled_notes_voice_action"
REMOTE_CONNECTIONS: dict[str, tuple[float, dict[str, object], Path]] = {}
REMOTE_CONNECTION_LOCK = threading.Lock()


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA_DIR / "drafts.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS drafts ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
        "body TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(drafts)")}
    if "title" not in columns:
        connection.execute("ALTER TABLE drafts ADD COLUMN title TEXT NOT NULL DEFAULT 'Untitled'")
        for row in connection.execute("SELECT id, body FROM drafts"):
            first_line = next((line.strip() for line in row["body"].splitlines() if line.strip()), "")
            connection.execute(
                "UPDATE drafts SET title = ? WHERE id = ?", (first_line[:120] or "Untitled", row["id"])
            )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS destination_defaults (id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS fired_actions ("
        "id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, "
        "destination TEXT NOT NULL, state TEXT NOT NULL, chat_id TEXT NOT NULL DEFAULT '', "
        "error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def draft_view(row: sqlite3.Row) -> dict[str, str | float]:
    return {
        "id": str(row["id"]),
        "body": str(row["body"]),
        "title": str(row["title"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def fields_from_request() -> tuple[str, str | None]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), str):
        abort(400, "Draft text is required")
    title = payload.get("title")
    if title is not None:
        if not isinstance(title, str) or len(title) > 120:
            abort(400, "Draft title must be text under 121 characters")
        title = title.strip()
    return payload["body"], title


@app.after_request
def prevent_caching(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def index() -> Response:
    html = (Path(__file__).parent / "assets" / "app.html").read_text()
    response = Response(html.replace("<!-- shell script -->", SHELL_PAGE_SCRIPT), mimetype="text/html")
    # Cross-origin isolation, which the Moonshine v2 dictation models' multithreaded
    # runtime needs (SharedArrayBuffer). It takes effect when the page is its own tab;
    # every outside resource the page loads (jsDelivr, Hugging Face, Moonshine's model
    # host) allows cross-origin use, so nothing it loads is refused.
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response


@app.get("/api/drafts")
def list_drafts() -> Response:
    with database() as connection:
        rows = connection.execute("SELECT * FROM drafts ORDER BY updated_at DESC").fetchall()
    return jsonify({"drafts": [draft_view(row) for row in rows]})


@app.post("/api/drafts")
def create_draft() -> Response:
    body, title = fields_from_request()
    draft_id = str(uuid.uuid4())
    now = time.time()
    with database() as connection:
        connection.execute(
            "INSERT INTO drafts (id, title, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (draft_id, title if title is not None else "", body, now, now),
        )
        row = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    assert row is not None
    return jsonify(draft_view(row)), 201


@app.patch("/api/drafts/<draft_id>")
def update_draft(draft_id: str) -> Response:
    body, title = fields_from_request()
    with database() as connection:
        updated = connection.execute(
            "UPDATE drafts SET title = COALESCE(?, title), body = ?, updated_at = ? WHERE id = ?",
            (title, body, time.time(), draft_id),
        )
        if updated.rowcount == 0:
            abort(404, "Draft not found")
        row = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    assert row is not None
    return jsonify(draft_view(row))


@app.delete("/api/drafts/<draft_id>")
def delete_draft(draft_id: str) -> Response:
    with database() as connection:
        deleted = connection.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        if deleted.rowcount == 0:
            abort(404, "Draft not found")
    return jsonify({"deleted": draft_id})


def action_view(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"], "title": row["title"], "body": row["body"],
        "destination": json.loads(row["destination"]), "state": row["state"],
        "chat_id": row["chat_id"], "error": row["error"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def set_action(action_id: str, state: str, *, chat_id: str = "", error: str = "") -> None:
    with database() as connection:
        connection.execute(
            "UPDATE fired_actions SET state = ?, chat_id = COALESCE(NULLIF(?, ''), chat_id), "
            "error = ?, updated_at = ? WHERE id = ?",
            (state, chat_id, error[:500], time.time(), action_id),
        )


def gateway_headers() -> dict[str, str]:
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    if not password:
        raise OSError("Linked machines are unavailable")
    headers = {"X-Latchkey-Gateway-Password": password}
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if override:
        headers["X-Latchkey-Gateway-Permissions-Override"] = override
    return headers


def remote_connection(machine: str) -> tuple[dict[str, object], Path]:
    # One grant/key per host, reused across the simultaneous model warmups.
    with REMOTE_CONNECTION_LOCK:
        cached = REMOTE_CONNECTIONS.get(machine)
        if cached and cached[0] > time.time():
            return cached[1], cached[2]
        connection, key = request_remote_connection(machine)
        REMOTE_CONNECTIONS[machine] = (time.time() + 20 * 60 * 60, connection, key)
        return connection, key


def request_remote_connection(machine: str) -> tuple[dict[str, object], Path]:
    gateway = os.environ.get("LATCHKEY_GATEWAY", "")
    if not gateway:
        raise OSError("Linked machines are unavailable")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = DATA_DIR / "machine_key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=10)
    public_key = key.with_suffix(".pub").read_text().strip()
    payload = json.dumps({"public_key": public_key, "requester_workspace_id": os.environ.get("MNGR_AGENT_ID", "")}).encode()
    url = f"{gateway.rstrip('/')}{STUDIO_WORKSPACES_PATH}/{urllib.parse.quote(machine, safe='')}/ssh"
    headers = {**gateway_headers(), "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers, method="POST"), timeout=45) as response:
        connection = json.load(response)
    if not isinstance(connection, dict) or not all(connection.get(field) for field in ("host", "port", "user")):
        raise OSError("Could not reach selected machine")
    return connection, key


def chat_request(machine: str, method: str, path: str, payload: dict | None = None, timeout: int = 45) -> tuple[int, object]:
    current = os.environ.get("MNGR_AGENT_ID", "")
    encoded = json.dumps(payload).encode() if payload is not None else None
    if not machine or machine == current:
        headers = {"Content-Type": "application/json"} if encoded is not None else {}
        req = urllib.request.Request(f"{CHAT_BASE_URL}{path}", data=encoded, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("detail", "Chat rejected the request")
            except (ValueError, AttributeError):
                detail = "Chat rejected the request"
            return exc.code, {"detail": detail}
    connection, key = remote_connection(machine)
    # Request contents travel on stdin; no user text is interpolated into the remote shell.
    script = (
        "import json,urllib.request,urllib.error,sys; "
        "r=json.load(sys.stdin); data=json.dumps(r['body']).encode() if r['body'] is not None else None; "
        "q=urllib.request.Request('http://127.0.0.1:8010'+r['path'],data=data,method=r['method'],"
        "headers={'Content-Type':'application/json'} if data is not None else {}); "
        "\ntry:\n with urllib.request.urlopen(q,timeout=r['timeout']) as x: print(json.dumps({'status':x.status,'body':json.load(x)}))"
        "\nexcept urllib.error.HTTPError as x: print(json.dumps({'status':x.code,'body':json.load(x)}))"
    )
    command = [
        "ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=12", "-p", str(connection["port"]),
        f"{connection['user']}@{connection['host']}", f"python3 -c {shlex.quote(script)}",
    ]
    result = subprocess.run(
        command, input=json.dumps({"path": path, "method": method, "body": payload, "timeout": timeout}),
        text=True, capture_output=True, timeout=timeout + 20, check=False,
    )
    if result.returncode:
        with REMOTE_CONNECTION_LOCK:
            REMOTE_CONNECTIONS.pop(machine, None)
        raise OSError("Selected machine did not respond")
    response = json.loads(result.stdout)
    return int(response["status"]), response["body"]


def deliver_action(action_id: str) -> None:
    with database() as connection:
        row = connection.execute("SELECT * FROM fired_actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return
    destination = json.loads(row["destination"])
    machine = destination.get("machine", "")
    try:
        if destination["mode"] == "new":
            status, accounts = chat_request(machine, "GET", "/api/accounts", timeout=12)
            if status != 200 or not isinstance(accounts, dict):
                raise ValueError("Accounts are unavailable on the selected machine")
            harness = PROVIDER_HARNESSES[destination["provider"]]
            account = next((item for item in accounts.get("accounts", []) if item.get("harness") == harness), None)
            if account is None:
                raise ValueError("Provider is not connected on the selected machine")
            create = {
                "name": destination.get("chat_name", ""), "account_id": account["id"],
                "message": row["body"], "should_wait": True,
                "labels": {ACTION_LABEL: action_id},
                "model": {"model_id": destination["model"], "effort": destination.get("effort") or None, "fast": False},
            }
            status, result = chat_request(machine, "POST", "/api/chats/create", create, timeout=325)
            if status == 201 and isinstance(result, dict) and result.get("chat_id"):
                set_action(action_id, "landed", chat_id=result["chat_id"])
            elif status == 504:
                set_action(action_id, "checking")
            else:
                set_action(action_id, "attention", error=result.get("detail", "Chat could not be created") if isinstance(result, dict) else "Chat could not be created")
        else:
            target = urllib.parse.quote(destination["chat_id"], safe="")
            status, result = chat_request(machine, "POST", f"/api/chats/{target}/message", {"message": row["body"], "message_id": action_id}, timeout=45)
            if status in (200, 202):
                set_action(action_id, "landed", chat_id=destination["chat_id"])
            else:
                set_action(action_id, "attention", error=result.get("detail", "Chat did not accept the message") if isinstance(result, dict) else "Chat did not accept the message")
    except (ValueError, KeyError) as exc:
        set_action(action_id, "attention", error=str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        # A timeout can follow a successful send. Leave it visible for inspection, never resend automatically.
        set_action(action_id, "checking", error=str(exc))


def reconcile_action(row: sqlite3.Row) -> None:
    destination = json.loads(row["destination"])
    elapsed = time.time() - row["created_at"]
    if destination["mode"] == "new":
        try:
            status, result = chat_request(destination.get("machine", ""), "GET", "/api/chats", timeout=8)
            if status != 200 or not isinstance(result, dict):
                return
            matching = next(
                (chat for chat in result.get("chats", []) if chat.get("labels", {}).get(ACTION_LABEL) == row["id"]), None
            )
            if matching:
                state = matching.get("active_agent", {}).get("state")
                if state == "RUNNING":
                    set_action(row["id"], "landed", chat_id=matching["chat_id"])
                elif matching.get("status") == "error":
                    set_action(row["id"], "attention", chat_id=matching["chat_id"], error="Chat could not start. Your text is safe here.")
                return
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return
    if elapsed > 360:
        set_action(row["id"], "attention", error="Delivery could not be confirmed. Your text is safe here.")


def check_uncertain_actions() -> None:
    with database() as connection:
        connection.execute("UPDATE fired_actions SET state = 'checking' WHERE state = 'sending'")
    while True:
        try:
            with database() as connection:
                pending = connection.execute(
                    "SELECT * FROM fired_actions WHERE state = 'checking' ORDER BY created_at DESC LIMIT 20"
                ).fetchall()
            for row in pending:
                reconcile_action(row)
        except (OSError, sqlite3.Error, ValueError):
            pass
        time.sleep(15)


@app.post("/api/fire")
def fire_draft() -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("draft_id"), str) or not isinstance(payload.get("destination"), dict):
        abort(400, "Choose a draft and destination")
    destination = payload["destination"]
    if destination.get("mode") not in ("new", "follow-up"):
        abort(400, "Choose a destination")
    if destination["mode"] == "new" and (
        destination.get("provider") not in PROVIDER_HARNESSES or
        not isinstance(destination.get("model"), str) or not destination["model"] or
        not isinstance(destination.get("machine"), str) or
        not isinstance(destination.get("chat_name", ""), str)
    ):
        abort(400, "Complete the chat settings")
    if destination["mode"] == "follow-up" and (
        not isinstance(destination.get("chat_id"), str) or not destination["chat_id"] or
        not isinstance(destination.get("machine", ""), str)
    ):
        abort(400, "Choose a chat")
    action_id = str(uuid.uuid4())
    with database() as connection:
        draft = connection.execute("SELECT * FROM drafts WHERE id = ?", (payload["draft_id"],)).fetchone()
        if draft is None:
            abort(404, "Draft not found")
        if not draft["body"].strip():
            abort(400, "Write something first")
        now = time.time()
        connection.execute(
            "INSERT INTO fired_actions (id, draft_id, title, body, destination, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'sending', ?, ?)",
            (action_id, draft["id"], draft["title"], draft["body"], json.dumps(destination), now, now),
        )
        connection.execute("DELETE FROM drafts WHERE id = ?", (draft["id"],))
        row = connection.execute("SELECT * FROM fired_actions WHERE id = ?", (action_id,)).fetchone()
    threading.Thread(target=deliver_action, args=(action_id,), daemon=True).start()
    return jsonify(action_view(row)), 202


@app.get("/api/fired")
def list_fired() -> Response:
    with database() as connection:
        rows = connection.execute("SELECT * FROM fired_actions WHERE state != 'landed' ORDER BY created_at DESC").fetchall()
    actions = []
    for row in rows:
        item = action_view(row)
        if item["state"] == "sending" and time.time() - float(item["updated_at"]) > 10:
            item["state"] = "checking"
        actions.append(item)
    return jsonify({"actions": actions})


def chat_json(path: str) -> object:
    try:
        with urllib.request.urlopen(f"{CHAT_BASE_URL}{path}", timeout=5) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        abort(503, "Chat choices are temporarily unavailable")


def studio_workspaces() -> dict[str, object] | None:
    gateway = os.environ.get("LATCHKEY_GATEWAY", "")
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    if not gateway or not password:
        return None
    headers = {"X-Latchkey-Gateway-Password": password}
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if override:
        headers["X-Latchkey-Gateway-Permissions-Override"] = override
    url = f"{gateway.rstrip('/')}{STUDIO_WORKSPACES_PATH}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=4) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@app.get("/api/machines")
def linked_machines() -> Response:
    current_id = os.environ.get("MNGR_AGENT_ID", "")
    payload = studio_workspaces()
    machines = []
    if payload and isinstance(payload.get("workspaces"), list):
        for row in payload["workspaces"]:
            if not isinstance(row, dict) or not isinstance(row.get("agent_id"), str):
                continue
            if row.get("host_state") != "RUNNING" and row["agent_id"] != current_id:
                continue
            machines.append({"id": row["agent_id"], "name": row.get("name") or "Unnamed machine"})
    if current_id and not any(machine["id"] == current_id for machine in machines):
        machines.append({"id": current_id, "name": "This machine"})
    machines.sort(key=lambda machine: (machine["id"] != current_id, machine["name"].casefold()))
    return jsonify({"machines": machines, "current_machine_id": current_id, "unavailable": payload is None})


@app.get("/api/chat-models/<provider>")
def chat_models(provider: str) -> Response:
    harness = PROVIDER_HARNESSES.get(provider)
    if harness is None:
        abort(404)
    machine = request.args.get("machine", "")

    def choices(path: str) -> object:
        if not machine or machine == os.environ.get("MNGR_AGENT_ID", ""):
            return chat_json(path)
        try:
            status, payload = chat_request(machine, "GET", path, timeout=15)
        except (OSError, ValueError, subprocess.SubprocessError):
            abort(503, "Selected machine's models are unavailable")
        if status != 200:
            abort(503, "Selected machine's models are unavailable")
        return payload

    catalogs = choices("/api/harnesses")
    accounts_payload = choices("/api/accounts")
    if not isinstance(catalogs, dict) or not isinstance(accounts_payload, (dict, list)):
        abort(503, "Chat choices are temporarily unavailable")
    accounts = accounts_payload if isinstance(accounts_payload, list) else accounts_payload.get("accounts", [])
    account = next((item for item in accounts if item.get("harness") == harness), None)
    catalog = catalogs.get(harness, {})
    options = catalog.get("options", [])
    if account is not None:
        account_id = urllib.parse.quote(account["id"], safe="")
        account_options = choices(f"/api/accounts/{account_id}/model-options")
        if isinstance(account_options, dict) and account_options.get("options") is not None:
            options = account_options["options"]
    models = [
        {
            "id": option["id"],
            "label": option["label"],
            "efforts": [effort["level"] for effort in option.get("efforts", []) if effort.get("in_picker")],
        }
        for option in options
        if option.get("in_picker")
    ]
    return jsonify({"provider": provider, "harness": harness, "available": account is not None, "models": models})


@app.get("/api/chat-names")
def chat_names() -> Response:
    # The chats on the selected machine (this one by default), for follow-ups.
    machine = request.args.get("machine", "")
    if not machine or machine == os.environ.get("MNGR_AGENT_ID", ""):
        payload = chat_json("/api/chats")
    else:
        try:
            status, payload = chat_request(machine, "GET", "/api/chats", timeout=15)
        except (OSError, ValueError, subprocess.SubprocessError):
            abort(503, "Selected machine's chats are unavailable")
        if status != 200:
            abort(503, "Selected machine's chats are unavailable")
    if not isinstance(payload, dict):
        abort(503, "Chat names are temporarily unavailable")
    chats = [
        {"id": chat["chat_id"], "name": chat.get("title") or chat.get("name") or "Chat"}
        for chat in payload.get("chats", [])
    ]
    chats.sort(key=lambda chat: chat["name"].casefold())
    return jsonify({"chats": chats})


@app.get("/api/destination-defaults")
def get_destination_defaults() -> Response:
    with database() as connection:
        row = connection.execute("SELECT value FROM destination_defaults WHERE id = 1").fetchone()
    return jsonify(json.loads(row["value"]) if row else {"provider": "claude", "by_provider": {}})


@app.put("/api/destination-defaults")
def save_destination_defaults() -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("provider") not in PROVIDER_HARNESSES:
        abort(400, "Choose a provider")
    by_provider = payload.get("by_provider")
    if not isinstance(by_provider, dict) or set(by_provider) - set(PROVIDER_HARNESSES):
        abort(400, "Invalid provider defaults")
    if not isinstance(payload.get("machine", ""), str) or len(payload.get("machine", "")) > 120:
        abort(400, "Invalid machine default")
    if not isinstance(payload.get("machine_name", ""), str) or len(payload.get("machine_name", "")) > 120:
        abort(400, "Invalid machine name")
    for settings in by_provider.values():
        if not isinstance(settings, dict) or not isinstance(settings.get("model", ""), str):
            abort(400, "Invalid model default")
        efforts = settings.get("effort_by_model", {})
        if not isinstance(efforts, dict) or any(
            not isinstance(model, str) or not isinstance(level, str) for model, level in efforts.items()
        ):
            abort(400, "Invalid effort defaults")
    encoded = json.dumps(payload)
    if len(encoded) > 65536:
        abort(400, "Too many defaults")
    with database() as connection:
        connection.execute(
            "INSERT INTO destination_defaults (id, value) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET value = excluded.value",
            (encoded,),
        )
    return jsonify(payload)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


# The on-device voice modules (see assets/voice_capture.js), served from this
# origin so the page loads them as ordinary scripts. Only these two names resolve.
VOICE_SCRIPT_NAMES = ("voice_session.js", "voice_capture.js", "voice_streaming.js")


@app.route("/voice/<basename>")
def voice_script(basename: str) -> Response:
    if basename not in VOICE_SCRIPT_NAMES:
        abort(404)
    return send_file(Path(__file__).parent / "assets" / basename, mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    threading.Thread(target=check_uncertain_actions, daemon=True).start()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
