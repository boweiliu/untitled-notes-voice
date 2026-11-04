"""Machine/provider/subscription preference boundaries, including migration and fresh browsers."""
import json
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from prompt_notes import preferences, runner


def test_legacy_preferences_belong_only_to_their_last_machine() -> None:
    old = {"machine": "one", "provider": "pi-dev", "by_provider": {"pi-dev": {
        "subscription": "claude", "model": "opus", "model_by_subscription": {"claude": "opus", "codex": "gpt"},
        "effort_by_model": {"opus": "high", "gpt": "low"}}}}
    saved = preferences.normalize(old)
    assert set(saved["by_machine"]) == {"one"}
    pi = saved["by_machine"]["one"]["by_provider"]["pi-dev"]
    assert pi["by_subscription"]["claude"]["model"] == "opus"
    assert pi["by_subscription"]["codex"]["effort_by_model"]["gpt"] == "low"
    assert "by_machine" not in old


def test_old_or_stale_clients_cannot_erase_other_machines(monkeypatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    client = runner.app.test_client()
    first = {"machine": "one", "provider": "codex", "by_provider": {"codex": {"model": "gpt", "effort_by_model": {"gpt": "high"}}}}
    assert client.put("/api/destination-defaults", json=first).status_code == 200
    stale = client.get("/api/destination-defaults").get_json()
    second = {"machine": "two", "provider": "claude", "by_provider": {"claude": {"model": "opus", "effort_by_model": {"opus": "low"}}}}
    assert client.put("/api/destination-defaults", json=second).status_code == 200
    stale["by_machine"]["one"]["by_provider"]["codex"]["effort_by_model"]["gpt"] = "max"
    assert client.put("/api/destination-defaults", json=stale).status_code == 200
    saved = client.get("/api/destination-defaults").get_json()
    assert saved["by_machine"]["two"]["by_provider"]["claude"]["model"] == "opus"
    assert saved["by_machine"]["one"]["by_provider"]["codex"]["effort_by_model"]["gpt"] == "max"
    # An unrefreshed legacy tab writes only its selected machine, too.
    assert client.put("/api/destination-defaults", json=second).status_code == 200
    assert len(client.get("/api/destination-defaults").get_json()["by_machine"]) == 2


@pytest.mark.parametrize("patch", [
    {"by_machine": []},
    {"by_machine": {"one": {"provider": "invalid", "by_provider": {}}}},
    {"by_machine": {"one": {"provider": "pi-dev", "by_provider": {"pi-dev": {"by_subscription": {"claude": {"effort_by_model": {"opus": 1}}}}}}}},
])
def test_invalid_nested_preferences_are_rejected(patch) -> None:
    assert runner.app.test_client().put("/api/destination-defaults", json={"machine": "one", **patch}).status_code == 400


@pytest.mark.browser
def test_switching_machines_providers_and_subscriptions_restores_each_scopes_choices(tmp_path) -> None:
    client = runner.app.test_client()
    client.put("/api/destination-defaults", json={"machine": "one", "provider": "codex", "by_provider": {
        "codex": {"model": "codex-two", "effort_by_model": {"codex-two": "high"}}}})
    runner.remember_choice(tmp_path, "machines", {"machines": [{"id": "one", "name": "One"}, {"id": "two", "name": "Two"}],
        "current_machine_id": "one", "unavailable": False})
    for machine in ("one", "two"):
        for provider in runner.PROVIDER_HARNESSES:
            models = [{"id": provider + "-" + name, "label": name, "efforts": ["low", "high"]} for name in ("one", "two")]
            value = {"provider": provider, "available": True, "models": models}
            if provider == "pi-dev":
                value["subscriptions"] = [{"id": name, "label": name, "available": True, "account_id": name} for name in ("a", "b")]
                value["models"] = [{"id": "shared-model", "label": "Shared", "efforts": ["low", "high"], "subscription": name} for name in ("a", "b")]
            runner.remember_choice(tmp_path, f"models-v4:{machine}:{provider}", value)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion", args=["--no-sandbox"])
            def new_page(stale_cache=False):
                page = browser.new_page()
                if stale_cache:
                    page.add_init_script("""localStorage.setItem('prompt-notes.defaults.v2', JSON.stringify({
                      version: 2, machine: 'one', provider: 'codex', updated_at: Date.now() + 86400000,
                      by_provider: {codex: {model: 'codex-one', effort_by_model: {}}},
                      by_machine: {one: {provider: 'codex', by_provider: {codex: {model: 'codex-one', effort_by_model: {}}}}}
                    }));""")
                page.route("**/api/chat-models/*", lambda route: route.abort())
                page.route("**/api/fire", lambda route: route.abort())
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.locator("#draft-text").fill("Scope check")
                page.locator("#continue-button").click()
                return page
            page = new_page(stale_cache=True)
            expect(page.locator("#model")).to_have_value("codex-two")
            expect(page.locator("#effort")).to_have_value("high")
            page.locator("#provider").select_option("claude")
            page.locator("#model").select_option("claude-two")
            page.locator("#effort").select_option("low")
            page.locator("#provider").select_option("codex")
            expect(page.locator("#model")).to_have_value("codex-two")
            expect(page.locator("#effort")).to_have_value("high")
            page.locator("#machine").select_option("two")
            expect(page.locator("#provider")).to_have_value("claude")
            expect(page.locator("#model")).to_have_value("claude-one")
            page.locator("#model").select_option("claude-two")
            page.locator("#effort").select_option("high")
            page.locator("#provider").select_option("codex")
            expect(page.locator("#model")).to_have_value("codex-one")
            expect(page.locator("#effort")).to_have_value("low")
            page.locator("#provider").select_option("pi-dev")
            page.locator("#subscription").select_option("a")
            page.locator("#effort").select_option("low")
            page.locator("#subscription").select_option("b")
            page.locator("#effort").select_option("high")
            page.locator("#subscription").select_option("a")
            expect(page.locator("#effort")).to_have_value("low")
            page.locator("#machine").select_option("one")
            expect(page.locator("#provider")).to_have_value("codex")
            expect(page.locator("#model")).to_have_value("codex-two")
            expect(page.locator("#effort")).to_have_value("high")
            page.evaluate("defaultsWrite")
            page.screenshot(path=str(tmp_path / "hierarchical-defaults.png"))
            # Brand-new storage reads the hierarchy from the server.
            fresh = new_page()
            expect(fresh.locator("#provider")).to_have_value("codex")
            expect(fresh.locator("#model")).to_have_value("codex-two")
            fresh.locator("#machine").select_option("two")
            expect(fresh.locator("#provider")).to_have_value("pi-dev")
            expect(fresh.locator("#subscription")).to_have_value("a")
            expect(fresh.locator("#effort")).to_have_value("low")
            fresh.locator("#subscription").select_option("b")
            expect(fresh.locator("#effort")).to_have_value("high")
            fresh.locator("#provider").select_option("claude")
            expect(fresh.locator("#model")).to_have_value("claude-two")
            expect(fresh.locator("#effort")).to_have_value("high")
            fresh.locator("#machine").select_option("one")
            fresh.locator("#provider").select_option("claude")
            expect(fresh.locator("#model")).to_have_value("claude-two")
            expect(fresh.locator("#effort")).to_have_value("low")
            assert client.get("/api/fired").get_json()["actions"] == []
            browser.close()
    finally:
        server.shutdown()
