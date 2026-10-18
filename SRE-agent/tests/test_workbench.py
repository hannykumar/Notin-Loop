from fastapi.testclient import TestClient

from runtime.api import app
from runtime.settings import reset_settings_cache


def test_workbench_assets_and_protected_data(monkeypatch):
    monkeypatch.setenv("OPS_API_TOKENS", "read|viewer|Reviewer")
    reset_settings_cache()
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/scenarios").status_code == 401
    response = client.get("/scenarios", headers={"X-API-Token": "read"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 5
    assert client.post("/evaluations", headers={"X-API-Token": "read"}, json={}).status_code == 403
    reset_settings_cache()


def test_evaluation_isolates_settings_and_mock_state(monkeypatch, tmp_path):
    import runtime.evaluations as evaluations

    calls = []
    class Process:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
        def poll(self):
            return None

    monkeypatch.setattr(evaluations, "OUTPUT", tmp_path / "result.json")
    monkeypatch.setattr(evaluations, "_process", None)
    monkeypatch.setattr(evaluations.subprocess, "Popen", Process)
    assert evaluations.start()["status"] == "running"
    command, kwargs = calls[0]
    assert command[command.index("--execution-mode") + 1] == "preview"
    assert kwargs["env"]["SRE_MCP_BACKEND"] == "mock"
    assert kwargs["env"]["SRE_MOCK_STATE_DIR"] == str(tmp_path / "mock_state")
    assert kwargs["env"]["DATABASE_URL"] == f"sqlite:///{tmp_path / 'evaluation.db'}"
