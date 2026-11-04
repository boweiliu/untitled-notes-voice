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

import base64
import hashlib
import html
import os
import json
import sqlite3
import subprocess
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from flask import Flask, Response, abort, has_request_context, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.serving import run_simple

from prompt_notes import agy_models
from prompt_notes import pi_subscriptions
from prompt_notes import retired_models
from prompt_notes import preferences

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
PRIMARY_MACHINE = os.environ.get("PROMPT_NOTES_PRIMARY_MACHINE", "")
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
# The one model offered for a provider whose model the chat app cannot set (Agy): a chat is
# created without a model pick and runs on the provider's own default.
PROVIDER_DEFAULT_MODEL = "provider-default"
# How long a send may stay unconfirmed before it needs attention.
CONFIRM_SECONDS = 360
# A send that has not finished after this long is shown as checking.
SENDING_SECONDS = 10
LOGGED_PROBLEMS: dict[str, str] = {}
REMOTE_CONNECTIONS: dict[str, tuple[float, dict[str, Any], Path]] = {}
REMOTE_CONNECTION_LOCK = threading.Lock()

# Machine, model and chat choices hardly ever change but are slow to collect from a
# linked machine (every lookup is an SSH round trip, the first also waits on a grant).
# The last answer is kept in memory and in DATA_DIR and returned at once; once it is
# older than CHOICE_REFRESH_SECONDS, a background refresh updates it for the next ask.
CHOICE_CACHE_FILE = "choice_cache.json"
CHOICE_REFRESH_SECONDS = 30.0
Choices = dict[str, Any]
CHOICE_CACHES: dict[Path, dict[str, tuple[float, Choices]]] = {}
CHOICE_REFRESHING: set[tuple[Path, str]] = set()
CHOICE_LOCK = threading.Lock()
CHOICE_COLLECT_LOCKS: dict[tuple[Path, str], threading.Lock] = {}


@contextmanager
def database() -> Generator[sqlite3.Connection, None, None]:
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
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(fired_actions)")}
    for column in ("retry_of", "retry_root_id"):
        if column not in columns:
            connection.execute(f"ALTER TABLE fired_actions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
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


# A machine that only follows another machine's notes (secondary-example follows primary-example, which
# holds the real ones) has mirror.json in its data folder, written by deploy/pull_data.py
# each time it copies the notes over. That copy is view-only: it refuses every change, and
# it never checks on sends, which belong to the machine that made them.
MIRROR_FILE = "mirror.json"


def mirror_record() -> dict[str, Any] | None:
    """The copy's details when this machine follows another one, else None.

    An unreadable mirror.json still means "copy": refusing a change by mistake is cheap,
    while a change made here would be overwritten by the next copy and lost.
    """
    path = DATA_DIR / MIRROR_FILE
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def mirror_source(record: dict[str, Any]) -> str:
    source = record.get("source")
    return source if isinstance(source, str) and source else "the main machine"


@app.before_request
def refuse_changes_to_a_copy() -> tuple[Response, int] | None:
    if request.method in ("GET", "HEAD", "OPTIONS") or not request.path.startswith("/api/"):
        return None
    record = mirror_record()
    if record is None:
        return None
    message = f"This is a view-only copy of the notes on {mirror_source(record)}. Make changes there."
    return jsonify({"description": message}), 409


def mirror_notice() -> str:
    record = mirror_record()
    if record is None:
        return ""
    source = html.escape(mirror_source(record))
    url = record.get("source_url")
    link = (
        f' <a href="{html.escape(url)}" target="_blank" rel="noopener">Open the notes on {source}</a>'
        if isinstance(url, str) and url.startswith("https://") else ""
    )
    return (
        '<aside id="mirror-notice" class="mirror-notice" aria-label="View-only copy">'
        f"<span>A view-only copy of the notes on {source}, copied over every 2 minutes. "
        f"Write and send there.</span>{link}</aside>"
    )


@app.after_request
def prevent_caching(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


# What browsers need to show this app with its own icon once it is installed (the
# desktop "Install app" and Android "Install" / "Add to home screen" flows read the
# web app manifest; none of them take an icon at install time). The images come from
# tools/build_pwa_icons.py. Their addresses carry a hash of the file, because Chromium
# only re-downloads an installed app's icon when the address in the manifest changes.
PWA_DIR = Path(__file__).parent / "assets" / "pwa"
PWA_ICON_NAMES = (
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-192.png",
    "icon-maskable-512.png",
    "apple-touch-icon.png",
)
PWA_PAGE_COLOR = "#faf9f5"
FRONTEND_DIR = Path(__file__).parent / "assets"


def frontend_version() -> str:
    """Content identity, read live so frontend edits need no server restart."""
    digest = hashlib.sha256()
    for path in sorted(FRONTEND_DIR.rglob("*")):
        if path.is_file() and ".test." not in path.name:
            digest.update(path.relative_to(FRONTEND_DIR).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


@app.get("/api/frontend-version")
def get_frontend_version() -> Response:
    response = jsonify({"version": frontend_version()})
    response.headers["Cache-Control"] = "no-store"
    return response


def pwa_icon_url(name: str) -> str:
    digest = hashlib.sha256((PWA_DIR / name).read_bytes()).hexdigest()[:10]
    return f"/pwa-icons/{name}?v={digest}"


def pwa_head_tags() -> str:
    favicon = base64.b64encode((PWA_DIR / "icon.svg").read_bytes()).decode("ascii")
    # The manifest request must carry the sign-in cookie: the forwarder in front of
    # the app refuses any request without one, and a manifest fetched without
    # credentials would be refused and silently ignored. The tab icon is inlined for
    # the same reason.
    return (
        '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">\n'
        f'  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{favicon}">\n'
        f'  <link rel="apple-touch-icon" href="{pwa_icon_url("apple-touch-icon.png")}">\n'
        f'  <meta name="theme-color" content="{PWA_PAGE_COLOR}">'
    )


@app.route("/")
def index() -> Response:
    version = frontend_version()
    page = (FRONTEND_DIR / "app.html").read_text()
    page = page.replace("__FRONTEND_VERSION__", version)
    bootstrap = json.dumps(selector_snapshot()).replace("<", "\\u003c")
    page = page.replace("<!-- selector bootstrap -->", f'<script id="selector-bootstrap" type="application/json">{bootstrap}</script>')
    page = page.replace("<!-- pwa head -->", pwa_head_tags())
    page = page.replace("<!-- mirror notice -->", mirror_notice())
    response = Response(page.replace("<!-- shell script -->", SHELL_PAGE_SCRIPT), mimetype="text/html")
    # Cross-origin isolation, which the Moonshine v2 dictation models' multithreaded
    # runtime needs (SharedArrayBuffer). It takes effect when the page is its own tab;
    # every outside resource the page loads (jsDelivr, Hugging Face, Moonshine's model
    # host) allows cross-origin use, so nothing it loads is refused.
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/drafts")
def list_drafts() -> Response:
    with database() as connection:
        rows = connection.execute("SELECT * FROM drafts ORDER BY updated_at DESC").fetchall()
    return jsonify({"drafts": [draft_view(row) for row in rows]})


@app.post("/api/drafts")
def create_draft() -> Response | tuple[Response, int]:
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


def action_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "body": row["body"],
        "destination": json.loads(row["destination"]), "state": row["state"],
        "chat_id": row["chat_id"], "error": row["error"],
        "retry_of": row["retry_of"], "retry_root_id": row["retry_root_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def log_event(message: str) -> None:
    # The supervisor keeps stderr; every line about a send carries its action id.
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", file=sys.stderr, flush=True)


def set_action(action_id: str, state: str, *, chat_id: str = "", error: str = "") -> None:
    with database() as connection:
        # A dismissed send stays dismissed, however late its delivery answers.
        connection.execute(
            "UPDATE fired_actions SET state = ?, chat_id = COALESCE(NULLIF(?, ''), chat_id), "
            "error = ?, updated_at = ? WHERE id = ? AND state NOT IN ('dismissed', 'superseded', 'landed')",
            (state, chat_id, error[:500], time.time(), action_id),
        )
        if state == "landed":
            row = connection.execute("SELECT * FROM fired_actions WHERE id = ?", (action_id,)).fetchone()
            if row is not None and row["state"] == "landed" and row["retry_of"]:
                connection.execute(
                    "UPDATE fired_actions SET state = 'superseded', updated_at = ? "
                    "WHERE (id = ? OR retry_root_id = ?) AND id != ? "
                    "AND state NOT IN ('landed', 'dismissed', 'superseded')",
                    (time.time(), row["retry_root_id"], row["retry_root_id"], action_id),
                )
    log_event(f"send {action_id} now {state}" + (f" chat={chat_id}" if chat_id else "") + (f" error={error[:200]!r}" if error else ""))


def gateway_headers() -> dict[str, str]:
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    if not password:
        raise OSError("Linked machines are unavailable")
    headers = {"X-Latchkey-Gateway-Password": password}
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if override:
        headers["X-Latchkey-Gateway-Permissions-Override"] = override
    return headers


@app.before_request
def primary_data_proxy() -> Response | None:
    """Configured copies use the primary machine for drafts, sends, defaults and catalogs; never fork the store."""
    if mirror_record() is not None:
        return None  # A declared view-only follower keeps its offline readable copy.
    if not PRIMARY_MACHINE or PRIMARY_MACHINE == os.environ.get("MNGR_AGENT_ID", ""):
        return None
    if request.path != "/" and not request.path.startswith("/api/"):
        return None
    if request.path == "/api/frontend-version":
        return None
    script = """import sys,json,base64,urllib.request,urllib.error
r=json.load(sys.stdin)
q=urllib.request.Request('http://127.0.0.1:8096'+r['path'],method=r['method'],
 data=base64.b64decode(r['body']) if r['body'] else None,headers={'Content-Type':r['content_type']})
try: x=urllib.request.urlopen(q,timeout=180)
except urllib.error.HTTPError as e: x=e
print(json.dumps({'status':x.status,'body':base64.b64encode(x.read()).decode(),
 'headers':{k:v for k,v in x.headers.items() if k.lower() in ('content-type','cache-control','cross-origin-opener-policy','cross-origin-embedder-policy')}}))
"""
    try:
        result = run_machine_python(PRIMARY_MACHINE, script, json.dumps({
            "path": request.full_path.rstrip("?"), "method": request.method,
            "body": base64.b64encode(request.get_data()).decode(),
            "content_type": request.content_type or "application/json",
        }), 200)
        answer = json.loads(result.stdout)
        return Response(base64.b64decode(answer["body"]), status=answer["status"], headers=answer["headers"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        abort(503, "The primary machine is unavailable. Your saved notes remain there; try again shortly.")


def destination_defaults() -> Choices:
    with database() as connection:
        row = connection.execute("SELECT value FROM destination_defaults WHERE id = 1").fetchone()
    value = json.loads(row["value"]) if row else {"provider": "claude", "by_provider": {}}
    return preferences.normalize(value, os.environ.get("MNGR_AGENT_ID", ""))


def selector_snapshot() -> Choices:
    """Only read durable caches: rendering a page never waits for model discovery."""
    with CHOICE_LOCK:
        entries = dict(choice_cache(DATA_DIR))
    return {
        "defaults": destination_defaults(),
        "machines": entries.get("machines", (0, None))[1],
        "catalogs": {key.removeprefix("models-v4:"): {"savedAt": saved_at * 1000, "data": without_retired(value)}
                     for key, (saved_at, value) in entries.items() if key.startswith("models-v4:")},
    }


@app.get("/api/choice-cache")
def get_selector_snapshot() -> Response:
    """Saved providers, models, efforts, machines and defaults, with no remote lookups."""
    response = jsonify(selector_snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


def choice_cache(data_dir: Path) -> dict[str, tuple[float, Choices]]:
    # The caller holds CHOICE_LOCK.
    cache = CHOICE_CACHES.get(data_dir)
    if cache is None:
        cache = {}
        try:
            loaded = json.loads((data_dir / CHOICE_CACHE_FILE).read_text())
        except (OSError, ValueError):
            loaded = {}
        for key, entry in loaded.items() if isinstance(loaded, dict) else ():
            if isinstance(entry, dict) and isinstance(entry.get("saved_at"), (int, float)) and isinstance(entry.get("value"), dict):
                cache[key] = (float(entry["saved_at"]), entry["value"])
        CHOICE_CACHES[data_dir] = cache
    return cache


def remember_choice(data_dir: Path, key: str, value: Choices) -> None:
    with CHOICE_LOCK:
        cache = choice_cache(data_dir)
        cache[key] = (time.time(), value)
        snapshot = {name: {"saved_at": saved_at, "value": saved} for name, (saved_at, saved) in cache.items()}
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            temporary = data_dir / f"{CHOICE_CACHE_FILE}.tmp"
            temporary.write_text(json.dumps(snapshot))
            temporary.replace(data_dir / CHOICE_CACHE_FILE)
        except OSError:
            pass  # The in-memory copy still serves; the file only speeds up a restart.


def refresh_choice_later(data_dir: Path, key: str, collect: Callable[[], Choices], keep: Callable[[Choices], bool]) -> None:
    with CHOICE_LOCK:
        if (data_dir, key) in CHOICE_REFRESHING:
            return
        CHOICE_REFRESHING.add((data_dir, key))

    def refresh() -> None:
        try:
            value = collect()
            if keep(value):
                remember_choice(data_dir, key, value)
        except (HTTPException, OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass  # Keep serving the last good answer.
        finally:
            with CHOICE_LOCK:
                CHOICE_REFRESHING.discard((data_dir, key))

    threading.Thread(target=refresh, daemon=True).start()


def cached_choice(
    key: str, collect: Callable[[], Choices], *, fresh: bool = False, keep: Callable[[Choices], bool] = lambda value: True
) -> Choices:
    """The last answer for ``key`` at once (refreshed behind it), or a first collection.

    ``fresh`` collects now (a refresh button); a failed or unkept collection then falls
    back to the last good answer when there is one.
    """
    data_dir = DATA_DIR
    with CHOICE_LOCK:
        entry = choice_cache(data_dir).get(key)
    if entry is not None and not fresh:
        saved_at, last = entry
        if time.time() - saved_at > CHOICE_REFRESH_SECONDS and not (has_request_context() and request.args.get("prefill") == "1"):
            refresh_choice_later(data_dir, key, collect, keep)
        return last
    with CHOICE_LOCK:
        collect_lock = CHOICE_COLLECT_LOCKS.setdefault((data_dir, key), threading.Lock())
    with collect_lock:
        with CHOICE_LOCK:
            latest = choice_cache(data_dir).get(key)
        if latest is not None and latest is not entry:
            return latest[1]
        try:
            value = collect()
        except (HTTPException, OSError, ValueError, KeyError, subprocess.SubprocessError):
            if entry is not None:
                return entry[1]
            raise
        if keep(value):
            remember_choice(data_dir, key, value)
        elif entry is not None:
            return entry[1]
        return value


def wants_fresh() -> bool:
    return request.args.get("fresh") == "1"


def remote_connection(machine: str) -> tuple[dict[str, Any], Path]:
    # One grant/key per host, reused across the simultaneous model warmups.
    with REMOTE_CONNECTION_LOCK:
        cached = REMOTE_CONNECTIONS.get(machine)
        if cached and cached[0] > time.time():
            return cached[1], cached[2]
        connection, key = request_remote_connection(machine)
        REMOTE_CONNECTIONS[machine] = (time.time() + 20 * 60 * 60, connection, key)
        return connection, key


def request_remote_connection(machine: str) -> tuple[dict[str, Any], Path]:
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
    # Request contents travel on stdin; no user text is interpolated into the remote shell.
    script = (
        "import json,urllib.request,urllib.error,sys; "
        "r=json.load(sys.stdin); data=json.dumps(r['body']).encode() if r['body'] is not None else None; "
        "q=urllib.request.Request('http://127.0.0.1:8010'+r['path'],data=data,method=r['method'],"
        "headers={'Content-Type':'application/json'} if data is not None else {}); "
        "\ntry:\n with urllib.request.urlopen(q,timeout=r['timeout']) as x: print(json.dumps({'status':x.status,'body':json.load(x)}))"
        "\nexcept urllib.error.HTTPError as x: print(json.dumps({'status':x.code,'body':json.load(x)}))"
    )
    result = run_machine_python(
        machine, script, json.dumps({"path": path, "method": method, "body": payload, "timeout": timeout}), timeout + 20
    )
    response = json.loads(result.stdout)
    return int(response["status"]), response["body"]


def run_machine_python(machine: str, script: str, stdin_text: str, timeout: int) -> subprocess.CompletedProcess:
    """Run ``python3 -c script`` on a linked machine (here, for this machine) with ``stdin_text`` on stdin."""
    if not machine or machine == os.environ.get("MNGR_AGENT_ID", ""):
        result = subprocess.run(
            ["python3", "-c", script], input=stdin_text, text=True, capture_output=True, timeout=timeout, check=False,
        )
        if result.returncode:
            raise OSError(f"This machine's helper failed: {result.stderr[-200:].strip()}")
        return result
    connection, key = remote_connection(machine)
    command = [
        "ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=12", "-p", str(connection["port"]),
        f"{connection['user']}@{connection['host']}", f"python3 -c {shlex.quote(script)}",
    ]
    result = subprocess.run(command, input=stdin_text, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        with REMOTE_CONNECTION_LOCK:
            REMOTE_CONNECTIONS.pop(machine, None)
        raise OSError("Selected machine did not respond")
    return result


AGY_REMOTE_SOURCE = (Path(__file__).parent / "agy_remote.py").read_text()
PI_REMOTE_SOURCE = (Path(__file__).parent / "pi_remote.py").read_text()


def machine_helper(machine: str, source: str, subject: str, request: dict, timeout: int) -> dict:
    """One JSON request to a helper script run on the machine that hosts the chats, and its JSON answer."""
    result = run_machine_python(machine, source, json.dumps(request), timeout)
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise OSError(f"Selected machine gave no answer about {subject}") from exc
    if not isinstance(answer, dict):
        raise OSError(f"Selected machine gave no answer about {subject}")
    return answer


def agy_remote(machine: str, request: dict, timeout: int) -> dict:
    """One request to ``agy_remote.py`` on the machine that hosts the Agy chats."""
    return machine_helper(machine, AGY_REMOTE_SOURCE, "Agy", request, timeout)


def pi_remote(machine: str, request: dict, timeout: int) -> dict:
    """One request to ``pi_remote.py`` on the machine that hosts the pi-dev chats."""
    return machine_helper(machine, PI_REMOTE_SOURCE, "pi-dev", request, timeout)


def agy_model_rows(machine: str, account_id: str) -> list[dict[str, str]]:
    """The models Agy lists on that machine for that account, as ``agy models`` prints them."""
    try:
        answer = agy_remote(machine, {"op": "models", "account_id": account_id}, timeout=75)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Agy's models are unavailable on the selected machine: {exc}") from exc
    rows = agy_models.parse_models(str(answer.get("text", ""))) if answer.get("ok") else []
    if not rows:
        raise ValueError(f"Agy's models are unavailable on the selected machine: {answer.get('detail') or 'no models listed'}")
    return rows


def chat_on_machine(machine: str, match: Callable[[dict], bool]) -> dict | None:
    status, payload = chat_request(machine, "GET", "/api/chats", timeout=20)
    if status != 200 or not isinstance(payload, dict):
        raise OSError(f"The machine's chat service answered {status}")
    return next((chat for chat in payload.get("chats", []) if isinstance(chat, dict) and match(chat)), None)


def chat_model(machine: str, chat_id: str) -> dict:
    """The model and effort the chat app reports an Agy chat as running (empty before it knows)."""
    chat = chat_on_machine(machine, lambda item: item.get("chat_id") == chat_id)
    choice = ((chat or {}).get("active_agent") or {}).get("model_choice") or {}
    identity = choice.get("identity") or {}
    return {"model_id": str(identity.get("model_id") or ""), "effort": str(identity.get("effort") or "")}


def await_created_chat(machine: str, action_id: str, seconds: float = 150) -> dict | None:
    """A chat whose create is still running (a 504): its identity once it is running, else None."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        chat = chat_on_machine(machine, lambda item: (item.get("labels") or {}).get(ACTION_LABEL) == action_id)
        if chat and (chat.get("active_agent") or {}).get("state") == "RUNNING":
            return {"chat_id": chat["chat_id"], "name": chat.get("name", "")}
        time.sleep(5)
    return None


def same_model(reported: dict, label: str) -> bool:
    """Whether the chat app's report is ``label`` ("Gemini 3.8 Flash (High)"). Agy up to 1.4.8 reported
    that whole label as the model; newer Agy reports the model and its effort apart ("Gemini 3.8 Flash", "high")."""
    model, effort = reported["model_id"].strip(), reported["effort"].strip()
    candidates = {model.casefold()} | ({f"{model} ({effort})".casefold()} if model and effort else set())
    return label.strip().casefold() in candidates


def put_agy_on_model(machine: str, chat_id: str, name: str, label: str, rows: list[dict[str, str]]) -> None:
    """Type the model change into the chat's Agy terminal and wait for the chat app to report it."""
    answer: dict = {}
    if not same_model(chat_model(machine, chat_id), label):
        answer = agy_remote(machine, {"op": "swap", "name": name, "label": label, **agy_models.swap_plan(label, rows)}, timeout=200)
        log_event(f"agy chat {chat_id} model change to {label!r}: {answer.get('detail')!r}")
    # The chat app reports a change a couple of seconds after the terminal shows it. When the terminal
    # said it failed, one more look still catches a model that was already the chosen one.
    deadline = time.time() + (30 if answer.get("ok", True) else 4)
    while True:
        if same_model(chat_model(machine, chat_id), label):
            return
        if time.time() > deadline:
            break
        time.sleep(2)
    raise ValueError(f"Agy would not switch to {label} ({answer.get('detail') or 'the chat app still shows the old model'})")


def deliver_agy_chat(action_id: str, row: sqlite3.Row, destination: dict, account: dict) -> None:
    """A new Agy chat on a chosen model: create it empty, change its model, check, then send the note.

    The chat app cannot set Agy's model (it only reads it), so the model is changed in the chat's own
    terminal and the chat app's report of it is the check. The note is sent only once the model is in place.
    """
    machine = destination.get("machine", "")
    rows = agy_model_rows(machine, account["id"])
    chosen = agy_models.find_row(rows, destination["model"], destination.get("effort") or "")
    if chosen is None:
        raise ValueError("Agy does not offer that model and effort on the selected machine")
    label = chosen["label"]
    deliver_after_model_change(
        action_id, row, destination, account, label,
        lambda chat_id, created: put_agy_on_model(machine, chat_id, created.get("name", ""), label, rows),
    )


def deliver_pi_chat(action_id: str, row: sqlite3.Row, destination: dict, account: dict) -> None:
    """A new pi-dev chat: created empty, put on its model and effort through pi's own model mailbox
    (the file the chat app itself writes for a switch), checked against pi's report, then sent the note.

    Not the chat app's model pick at create: it refuses models an extension adds (the Claude Code
    add-on's), and for a model without a "medium" effort (GLM-5.3: low, high, max) the effort it
    sets at start was seen to come out as another one. Set on the running chat, both land.
    """
    machine = destination.get("machine", "")
    label = destination.get("model_label") or destination["model"]
    if retired_model_refusal(destination):
        # A resend of an older note, or one fired before the model was retired: no empty chat is made.
        raise ValueError(retired_model_refusal(destination))

    def put_on_model(chat_id: str, created: dict) -> None:
        request_body = {"op": "apply", "chat_id": chat_id, "model_id": destination["model"], "thinking_level": destination.get("effort") or None}
        answer = pi_remote(machine, request_body, timeout=120)
        log_event(f"pi chat {chat_id} model change to {destination['model']!r}: {answer.get('detail')!r}")
        if not answer.get("ok"):
            raise ValueError(f"pi would not switch to {label} ({answer.get('detail') or 'no answer'})")

    deliver_after_model_change(action_id, row, destination, account, label, put_on_model)


def deliver_after_model_change(
    action_id: str, row: sqlite3.Row, destination: dict, account: dict, label: str,
    put_on_model: Callable[[str, dict], None],
) -> None:
    """Create an empty chat, ``put_on_model`` it, then send the note; an empty chat that could not be
    put on its model is removed rather than left behind."""
    machine = destination.get("machine", "")
    create = {
        "name": destination.get("chat_name", ""), "account_id": account["id"], "should_wait": True,
        "labels": {ACTION_LABEL: action_id},
    }
    status, created = chat_request(machine, "POST", "/api/chats/create", create, timeout=325)
    if status == 504:
        created = await_created_chat(machine, action_id) or {}
    elif status != 201:
        raise ValueError(created.get("detail", "Chat could not be created") if isinstance(created, dict) else "Chat could not be created")
    if not isinstance(created, dict) or not created.get("chat_id"):
        raise ValueError("Chat did not finish starting")
    chat_id = created["chat_id"]
    try:
        put_on_model(chat_id, created)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        # Nothing was sent to this chat yet, so the empty chat this made is removed rather than left behind.
        removed = False
        try:
            removed = chat_request(machine, "POST", f"/api/chats/{urllib.parse.quote(chat_id, safe='')}/destroy", timeout=60)[0] == 200
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        raise ValueError(f"{exc}. {'The new empty chat was removed' if removed else 'An empty chat was left on that machine'}") from exc
    target = urllib.parse.quote(chat_id, safe="")
    for _ in range(2):
        try:
            # The same message id both times: a retry after a lost answer cannot post the note twice.
            status, result = chat_request(machine, "POST", f"/api/chats/{target}/message", {"message": row["body"], "message_id": action_id}, timeout=45)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            continue
        if status in (200, 202):
            set_action(action_id, "landed", chat_id=chat_id)
            return
        raise ValueError(result.get("detail", "Chat did not accept the message") if isinstance(result, dict) else "Chat did not accept the message")
    raise ValueError(f"The chat was created on {label}, but the note may not have arrived")


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
            candidates = [item for item in accounts.get("accounts", []) if item.get("harness") == harness]
            # A pi-dev send names its subscription's account; an older one (or another provider) takes the first.
            account = next((item for item in candidates if item.get("id") == destination.get("account_id")), None)
            if account is None and destination.get("account_id"):
                raise ValueError("That subscription is no longer set up on the selected machine")
            account = account or (candidates[0] if candidates else None)
            if account is None:
                raise ValueError("Provider is not connected on the selected machine")
            if harness == "antigravity" and destination["model"] != PROVIDER_DEFAULT_MODEL:
                deliver_agy_chat(action_id, row, destination, account)
                return
            if harness == "pi-coding":
                deliver_pi_chat(action_id, row, destination, account)
                return
            create = {
                "name": destination.get("chat_name", ""), "account_id": account["id"],
                "message": row["body"], "should_wait": True,
                "labels": {ACTION_LABEL: action_id},
            }
            if destination["model"] != PROVIDER_DEFAULT_MODEL:
                create["model"] = {"model_id": destination["model"], "effort": destination.get("effort") or None, "fast": False}
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


def note_problem(action_id: str, problem: str) -> None:
    # One line per distinct problem, not one every check.
    if LOGGED_PROBLEMS.get(action_id) != problem:
        LOGGED_PROBLEMS[action_id] = problem
        log_event(f"send {action_id} cannot be confirmed yet: {problem}")


def reconcile_action(row: sqlite3.Row) -> None:
    destination = json.loads(row["destination"])
    elapsed = time.time() - row["created_at"]
    problem = ""
    if destination["mode"] == "new":
        try:
            status, result = chat_request(destination.get("machine", ""), "GET", "/api/chats", timeout=8)
            if status == 200 and isinstance(result, dict):
                matching = next(
                    (chat for chat in result.get("chats", []) if chat.get("labels", {}).get(ACTION_LABEL) == row["id"]), None
                )
                if matching:
                    state = matching.get("active_agent", {}).get("state")
                    if state == "RUNNING":
                        set_action(row["id"], "landed", chat_id=matching["chat_id"])
                    elif matching.get("status") == "error":
                        set_action(row["id"], "attention", chat_id=matching["chat_id"], error="Chat could not start. Your text is safe here.")
                    elif elapsed > CONFIRM_SECONDS:
                        # The chat exists and did not fail. It is just not running now: it
                        # finished its first turn, or was stopped since. Waiting on it forever helps no one.
                        set_action(row["id"], "landed", chat_id=matching["chat_id"])
                    return
            else:
                problem = f"the machine's chat service answered {status}"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            problem = str(exc) or type(exc).__name__
    if problem:
        note_problem(row["id"], problem)
    if elapsed > CONFIRM_SECONDS:
        # Keep the first reason too: the delivery attempt's error is what explains a stuck send.
        reason = "; ".join(dict.fromkeys(part for part in (row["error"], problem) if part))
        set_action(
            row["id"], "attention",
            error="Delivery could not be confirmed. Your text is safe here." + (f" Last problem: {reason}" if reason else ""),
        )


def check_uncertain_actions() -> None:
    if mirror_record() is None:
        with database() as connection:
            connection.execute("UPDATE fired_actions SET state = 'checking' WHERE state = 'sending'")
    while True:
        try:
            if mirror_record() is not None:
                time.sleep(15)
                continue
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
def fire_draft() -> Response | tuple[Response, int]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("draft_id"), str) or not isinstance(payload.get("destination"), dict):
        abort(400, "Choose a draft and destination")
    destination = payload["destination"]
    if destination.get("mode") not in ("new", "follow-up"):
        abort(400, "Choose a destination")
    if destination["mode"] == "new" and (
        destination.get("provider") not in PROVIDER_HARNESSES or
        not isinstance(destination.get("model"), str) or not destination["model"] or
        not isinstance(destination.get("model_label", ""), str) or len(destination.get("model_label", "")) > 120 or
        not isinstance(destination.get("machine"), str) or
        not isinstance(destination.get("chat_name", ""), str) or
        any(not isinstance(destination.get(key, ""), str) or len(destination.get(key, "")) > 120 for key in ("subscription", "subscription_label", "account_id"))
    ):
        abort(400, "Complete the chat settings")
    if destination["mode"] == "new" and retired_model_refusal(destination):
        abort(400, retired_model_refusal(destination))
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
    log_event(
        f"send {action_id} fired title={draft['title'][:60]!r} mode={destination['mode']} "
        f"machine={destination.get('machine_name') or destination.get('machine') or ''!r}"
    )
    threading.Thread(target=deliver_action, args=(action_id,), daemon=True).start()
    return jsonify(action_view(row)), 202


@app.get("/api/fired")
def list_fired() -> Response:
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM fired_actions WHERE state NOT IN ('landed', 'dismissed', 'superseded') ORDER BY created_at DESC"
        ).fetchall()
    actions = [shown_action(row) for row in rows]
    for item in actions:
        root = item["retry_root_id"] or item["id"]
        item["pending_retry_id"] = next((other["id"] for other in actions if other["retry_root_id"] == root
                                         and other["id"] != item["id"] and other["state"] in ("sending", "checking")), "")
    return jsonify({"actions": actions})


@app.post("/api/fired/<action_id>/resend")
def resend_fired(action_id: str) -> Response | tuple[Response, int]:
    """A fresh delivery of the same text and destination, retaining every attempt."""
    with database() as connection:
        # Serialize double clicks and retries from multiple browsers.
        connection.execute("BEGIN IMMEDIATE")
        original = connection.execute("SELECT * FROM fired_actions WHERE id = ?", (action_id,)).fetchone()
        if original is None or original["state"] in ("landed", "dismissed", "superseded"):
            abort(404, "That send is not in the list")
        if shown_action(original)["state"] == "sending":
            abort(409, "Still sending; wait a few seconds")
        root = original["retry_root_id"] or original["id"]
        pending = connection.execute(
            "SELECT * FROM fired_actions WHERE retry_root_id = ? AND id != ? AND state IN ('sending', 'checking') LIMIT 1",
            (root, action_id),
        ).fetchone()
        if pending is not None:
            return jsonify(action_view(pending)), 202
        retry_id = str(uuid.uuid4())
        now = time.time()
        connection.execute(
            "INSERT INTO fired_actions (id, draft_id, title, body, destination, state, created_at, updated_at, retry_of, retry_root_id) "
            "VALUES (?, ?, ?, ?, ?, 'sending', ?, ?, ?, ?)",
            (retry_id, original["draft_id"], original["title"], original["body"], original["destination"],
             now, now, action_id, root),
        )
        row = connection.execute("SELECT * FROM fired_actions WHERE id = ?", (retry_id,)).fetchone()
    log_event(f"send {retry_id} resends {action_id}")
    threading.Thread(target=deliver_action, args=(retry_id,), daemon=True).start()
    return jsonify(action_view(row)), 202


def shown_action(row: sqlite3.Row) -> dict[str, Any]:
    item = action_view(row)
    if item["state"] == "sending" and time.time() - float(item["updated_at"]) > SENDING_SECONDS:
        item["state"] = "checking"
    return item


@app.delete("/api/fired/<action_id>")
def dismiss_fired(action_id: str) -> Response:
    """Take a stuck send off the list. The record stays in the database, as a landed one does."""
    with database() as connection:
        row = connection.execute("SELECT * FROM fired_actions WHERE id = ?", (action_id,)).fetchone()
        if row is None or row["state"] in ("landed", "dismissed", "superseded"):
            abort(404, "That send is not in the list")
        if shown_action(row)["state"] == "sending":
            abort(409, "Still sending; wait a few seconds")
        connection.execute("UPDATE fired_actions SET state = 'dismissed', updated_at = ? WHERE id = ?", (time.time(), action_id))
    log_event(f"send {action_id} dismissed from the list (was {row['state']})")
    return jsonify({"dismissed": action_id})


def chat_json(path: str) -> object:
    try:
        with urllib.request.urlopen(f"{CHAT_BASE_URL}{path}", timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and path.endswith("/model-options"):
            return {}
        abort(503, "Chat choices are temporarily unavailable")
    except (OSError, ValueError, urllib.error.URLError):
        abort(503, "Chat choices are temporarily unavailable")


def studio_workspaces() -> dict[str, Any] | None:
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
    # An unavailable answer is not kept: the last good list stays in its place.
    return jsonify(cached_choice("machines", collect_machines, fresh=wants_fresh(), keep=lambda value: not value["unavailable"]))


def collect_machines() -> Choices:
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
    return {"machines": machines, "current_machine_id": current_id, "unavailable": payload is None}


@app.get("/api/chat-models/<provider>")
def chat_models(provider: str) -> Response:
    harness = PROVIDER_HARNESSES.get(provider)
    if harness is None:
        abort(404)
    machine = request.args.get("machine", "") or os.environ.get("MNGR_AGENT_ID", "")
    return jsonify(without_retired(cached_choice(
        f"models-v4:{machine}:{provider}", lambda: collect_models(provider, harness, machine), fresh=wants_fresh()
    )))


def without_retired(choices: object) -> object:
    """A model list minus the models OpenCode retired, so a list saved before they were retired never offers them."""
    if not isinstance(choices, dict) or not isinstance(choices.get("models"), list):
        return choices
    models = [model for model in choices["models"] if not (isinstance(model, dict) and model.get("id") in retired_models.RETIRED)]
    return {**choices, "models": models}


def retired_model_refusal(destination: dict) -> str:
    """Why a new pi-dev chat on this model would fail at once, or an empty string when it would not."""
    model = destination.get("model", "")
    if destination.get("provider") != "pi-dev" or model not in retired_models.RETIRED:
        return ""
    label = destination.get("model_label") or model
    instead = retired_models.replacement(model).partition("/")[2]
    return f"OpenCode has retired {label}; choose another model" + (f" (it suggests {instead})" if instead else "")


def collect_models(provider: str, harness: str, machine: str) -> Choices:
    def choices(path: str) -> object:
        if not machine or machine == os.environ.get("MNGR_AGENT_ID", ""):
            return chat_json(path)
        try:
            status, payload = chat_request(machine, "GET", path, timeout=15)
        except (OSError, ValueError, subprocess.SubprocessError):
            abort(503, "Selected machine's models are unavailable")
        if status == 404 and path.endswith("/model-options"):
            return {}  # Older chat services expose only the harness catalog.
        if status != 200:
            abort(503, "Selected machine's models are unavailable")
        return payload

    catalogs = choices("/api/harnesses")
    accounts_payload = choices("/api/accounts")
    if not isinstance(catalogs, dict) or not isinstance(accounts_payload, (dict, list)):
        abort(503, "Chat choices are temporarily unavailable")
    accounts = accounts_payload if isinstance(accounts_payload, list) else accounts_payload.get("accounts", [])
    if harness == "pi-coding":
        return collect_pi_models(provider, harness, machine, [item for item in accounts if item.get("harness") == harness])
    account = next((item for item in accounts if item.get("harness") == harness), None)
    catalog = catalogs.get(harness, {})
    if catalog.get("switch_mode") == "read_only":
        # The chat app only reads this provider's model (Agy's is changed in its terminal) and drops a
        # pick made for a new chat. A chat on the provider's own default is always on offer; Agy's other
        # models are what `agy models` lists on that machine, reached by changing the model in its terminal.
        models: list[dict[str, Any]] = []
        if account is not None:
            models.append({"id": PROVIDER_DEFAULT_MODEL, "label": f"{provider.capitalize()}'s own default model", "efforts": []})
            if harness == "antigravity":
                try:
                    models += agy_models.model_choices(agy_model_rows(machine, account["id"]))
                except ValueError as exc:
                    log_event(f"agy models on {machine or 'this machine'}: {exc}")
        return {"provider": provider, "harness": harness, "available": account is not None, "models": models}
    options = catalog.get("options", [])
    if account is not None:
        account_id = urllib.parse.quote(account["id"], safe="")
        account_options = choices(f"/api/accounts/{account_id}/model-options")
        # An account can answer with an empty list (Agy does); the catalog still holds its models.
        if isinstance(account_options, dict) and account_options.get("options"):
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
    return {"provider": provider, "harness": harness, "available": account is not None, "models": models}


def collect_pi_models(provider: str, harness: str, machine: str, pi_accounts: list[dict]) -> Choices:
    """pi-dev's subscriptions on that machine (one per pi account) and the models pi offers on each.

    pi's own list, read on the machine, rather than the chat app's catalog: the catalog lists every
    model pi knows (well over a thousand) whatever the account, and leaves out the ones an extension
    adds, which is how the Claude subscription's models arrive.
    """
    answers: dict[str, dict] = {}
    if pi_accounts:
        try:
            answer = pi_remote(machine, {"op": "subscriptions", "account_ids": [item["id"] for item in pi_accounts]}, timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            log_event(f"pi-dev subscriptions on {machine or 'this machine'}: {exc}")
            abort(503, "Selected machine's models are unavailable")
        if not answer.get("ok"):
            log_event(f"pi-dev subscriptions on {machine or 'this machine'}: {answer.get('detail')}")
            abort(503, "Selected machine's subscriptions are temporarily unavailable")
        answers = {item.get("account_id"): item for item in answer.get("accounts", []) if isinstance(item, dict)}
        if any(not answers.get(item["id"], {}).get("ok") for item in pi_accounts):
            abort(503, "Selected machine's subscriptions are temporarily unavailable")
    choices = pi_subscriptions.build_choices(pi_accounts, answers)
    return {"provider": provider, "harness": harness, "available": bool(choices["models"]), **choices}


@app.get("/api/chat-names")
def chat_names() -> Response:
    # The chats on the selected machine (this one by default), for follow-ups.
    machine = request.args.get("machine", "") or os.environ.get("MNGR_AGENT_ID", "")
    return jsonify(cached_choice(f"chats:{machine}", lambda: collect_chat_names(machine), fresh=wants_fresh()))


def collect_chat_names(machine: str) -> Choices:
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
    return {"chats": chats}


@app.get("/api/destination-defaults")
def get_destination_defaults() -> Response:
    response = jsonify(destination_defaults())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.put("/api/destination-defaults")
def save_destination_defaults() -> Response:
    payload = request.get_json(silent=True)
    try:
        preferences.validate(payload)
    except ValueError as error:
        abort(400, str(error))
    assert isinstance(payload, dict)
    if len(json.dumps(payload)) > 262144:
        abort(400, "Too many defaults")
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT value FROM destination_defaults WHERE id = 1").fetchone()
        existing = json.loads(row["value"]) if row else {"provider": "claude", "by_provider": {}}
        payload = preferences.merge(existing, payload, os.environ.get("MNGR_AGENT_ID", ""))
        encoded = json.dumps(payload)
        if len(encoded) > 262144:
            abort(400, "Too many defaults")
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


@app.get("/manifest.webmanifest")
def web_app_manifest() -> Response:
    manifest = {
        # A fixed id keeps an installed copy the same app if the start address ever changes.
        "id": "/",
        "name": "Untitled Notes Voice",
        "short_name": "Notes Voice",
        "description": "Notes with on-device voice dictation",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": PWA_PAGE_COLOR,
        "theme_color": PWA_PAGE_COLOR,
        "icons": [
            {"src": pwa_icon_url("icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": pwa_icon_url("icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {
                "src": pwa_icon_url("icon-maskable-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": pwa_icon_url("icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return Response(json.dumps(manifest), mimetype="application/manifest+json")


@app.route("/pwa-icons/<basename>")
def pwa_icon(basename: str) -> Response:
    if basename not in PWA_ICON_NAMES:
        abort(404)
    return send_file(PWA_DIR / basename, mimetype="image/png")


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
    if not PRIMARY_MACHINE or PRIMARY_MACHINE == os.environ.get("MNGR_AGENT_ID", ""):
        threading.Thread(target=check_uncertain_actions, daemon=True).start()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
