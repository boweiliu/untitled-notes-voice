"""Server selector caches and the authoritative-store boundary."""
import base64
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from prompt_notes import runner


def test_initial_page_contains_saved_choices_without_discovery(tmp_path, monkeypatch) -> None:
    machine = "worker"
    defaults = {"provider": "codex", "machine": machine, "by_provider": {
        "codex": {"model": "gpt", "effort_by_model": {"gpt": "high"}}}}
    client = runner.app.test_client()
    assert client.put("/api/destination-defaults", json=defaults).status_code == 200
    for provider in runner.PROVIDER_HARNESSES:
        runner.remember_choice(tmp_path, f"models-v4:{machine}:{provider}", {
            "provider": provider, "available": True,
            "models": [{"id": "gpt", "label": "</script><script>bad()</script>", "efforts": ["high"]}]})
    runner.remember_choice(tmp_path, "machines", {"machines": [{"id": machine, "name": "Worker"}],
        "current_machine_id": machine, "unavailable": False})
    runner.CHOICE_CACHES.clear()  # A restart keeps the cache.
    monkeypatch.setattr(runner, "collect_models", lambda *args: (_ for _ in ()).throw(AssertionError("live discovery")))
    html = client.get("/").get_data(as_text=True)
    assert 'id="selector-bootstrap"' in html
    assert "</script><script>bad()" not in html
    snapshot = client.get("/api/choice-cache").get_json()
    assert snapshot["defaults"]["by_machine"][machine] == {"provider": "codex", "by_provider": defaults["by_provider"]}
    assert len(snapshot["catalogs"]) == 4
    assert snapshot["catalogs"]["worker:codex"]["data"]["models"][0]["efforts"] == ["high"]


def test_prefill_never_refreshes_present_catalogs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "CHOICE_REFRESH_SECONDS", -1)
    runner.remember_choice(tmp_path, "models-v4:worker:codex", {"models": [{"id": "saved"}]})
    monkeypatch.setattr(runner, "collect_models", lambda *args: (_ for _ in ()).throw(AssertionError("overwrote present cache")))
    answer = runner.app.test_client().get("/api/chat-models/codex?machine=worker&prefill=1")
    assert answer.get_json()["models"] == [{"id": "saved"}]


def test_simultaneous_missing_reads_share_discovery(monkeypatch) -> None:
    gate = threading.Event()
    entered = threading.Event()
    calls = []
    def collect(*args):
        calls.append(1)
        entered.set()
        assert gate.wait(5)
        return {"models": [{"id": "new"}]}
    monkeypatch.setattr(runner, "collect_models", collect)
    def read():
        return runner.app.test_client().get("/api/chat-models/codex?machine=worker").get_json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(read)
        assert entered.wait(5)
        second = pool.submit(read)
        gate.set()
        assert first.result() == second.result() == {"models": [{"id": "new"}]}
    assert calls == [1]


def test_failed_pi_refresh_preserves_complete_subscription_cache(tmp_path, monkeypatch) -> None:
    saved = {"models": [{"id": "claude-code/opus"}], "subscriptions": [{"id": "claude", "available": True}]}
    runner.remember_choice(tmp_path, "models-v4:worker:pi-dev", saved)
    monkeypatch.setattr(runner, "chat_request", lambda *args, **kwargs: (200, {"accounts": [{"id": "a", "harness": "pi-coding"}]}))
    monkeypatch.setattr(runner, "pi_remote", lambda *args, **kwargs: {"ok": True, "accounts": [{"account_id": "a", "ok": False}]})
    assert runner.app.test_client().get("/api/chat-models/pi-dev?machine=worker&fresh=1").get_json() == saved
    assert json.loads((tmp_path / "choice_cache.json").read_text())["models-v4:worker:pi-dev"]["value"] == saved


def test_satellite_forwards_writes_and_fails_without_forking_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "PRIMARY_MACHINE", "talker")
    monkeypatch.setenv("MNGR_AGENT_ID", "satellite")
    asked = []
    def remote(machine, script, payload, timeout):
        asked.append((machine, json.loads(payload)))
        return SimpleNamespace(stdout=json.dumps({"status": 201, "body": base64.b64encode(b'{"id":"canonical"}').decode(),
            "headers": {"Content-Type": "application/json"}}))
    monkeypatch.setattr(runner, "run_machine_python", remote)
    answer = runner.app.test_client().post("/api/drafts", json={"body": "keep on talker"})
    assert answer.status_code == 201 and answer.get_json()["id"] == "canonical"
    assert asked[0][0] == "talker"
    assert json.loads(base64.b64decode(asked[0][1]["body"]))["body"] == "keep on talker"
    assert not (tmp_path / "drafts.sqlite3").exists()
    def offline(*args):
        raise OSError("offline")
    monkeypatch.setattr(runner, "run_machine_python", offline)
    assert runner.app.test_client().post("/api/drafts", json={"body": "no split"}).status_code == 503
    assert not (tmp_path / "drafts.sqlite3").exists()




import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


@pytest.mark.browser
def test_new_browser_uses_server_models_defaults_and_machines_with_apis_offline(tmp_path, monkeypatch) -> None:
    defaults = {"provider": "pi-dev", "machine": "worker", "by_provider": {"pi-dev": {
        "subscription": "claude", "model": "claude-code/opus", "effort_by_model": {"claude-code/opus": "high"}}}}
    runner.app.test_client().put("/api/destination-defaults", json=defaults)
    runner.remember_choice(tmp_path, "machines", {"machines": [{"id": "worker", "name": "Worker"}, {"id": "talker", "name": "Talker"}],
        "current_machine_id": "talker", "unavailable": False})
    for machine in ("worker", "talker"):
        for provider in runner.PROVIDER_HARNESSES:
            value = {"provider": provider, "available": True, "models": [{
                "id": "claude-code/opus" if provider == "pi-dev" else provider + "-model", "label": "Opus" if provider == "pi-dev" else provider,
                "efforts": ["low", "high"], "subscription": "claude"}]}
            if provider == "pi-dev":
                value["subscriptions"] = [{"id": "claude", "label": "Claude", "available": True, "account_id": "a"}]
            runner.remember_choice(tmp_path, f"models-v4:{machine}:{provider}", value)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/chat-models/*", lambda route: route.abort())
            page.route("**/api/machines*", lambda route: route.abort())
            page.route("**/api/destination-defaults", lambda route: route.abort() if route.request.method == "GET" else route.continue_())
            page.route("**/api/fire", lambda route: route.abort())
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.locator("#draft-text").fill("Cached picker check")
            page.locator("#continue-button").click()
            expect(page.locator("#machine")).to_have_value("worker")
            expect(page.locator("#provider")).to_have_value("pi-dev")
            expect(page.locator("#subscription")).to_have_value("claude")
            expect(page.locator("#model")).to_have_value("claude-code/opus")
            expect(page.locator("#effort")).to_have_value("high")
            for machine in ("talker", "worker"):
                page.locator("#machine").select_option(machine)
                for provider in runner.PROVIDER_HARNESSES:
                    page.locator("#provider").select_option(provider)
                    expect(page.locator("#model")).to_be_enabled()
            assert errors == []
            browser.close()
    finally:
        server.shutdown()




@pytest.mark.parametrize("provider,harness", [("claude", "claude"), ("codex", "codex"), ("agy", "antigravity")])
def test_older_machines_without_account_options_use_their_own_harness_catalog(monkeypatch, provider, harness) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "local")
    def remote(machine, method, path, **kwargs):
        if path == "/api/harnesses":
            return 200, {harness: {"options": [{"id": "own-model", "label": "Own model", "in_picker": True,
                "efforts": [{"level": "high", "in_picker": True}]}]}}
        if path == "/api/accounts":
            return 200, {"accounts": [{"id": "old-account", "harness": harness}]}
        return 404, {"detail": "No such endpoint"}
    monkeypatch.setattr(runner, "chat_request", remote)
    data = runner.app.test_client().get(f"/api/chat-models/{provider}?machine=old&prefill=1").get_json()
    assert data["available"] is True
    assert data["models"] == [{"id": "own-model", "label": "Own model", "efforts": ["high"]}]
