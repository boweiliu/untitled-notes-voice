"""Keep every test local even when the runner inherits a connected workspace."""
import pytest
from prompt_notes import runner


@pytest.fixture(autouse=True)
def isolated_destinations(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "CHAT_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    monkeypatch.delenv("LATCHKEY_GATEWAY_PASSWORD", raising=False)
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
    monkeypatch.setenv("MNGR_AGENT_ID", "local-test-machine")
