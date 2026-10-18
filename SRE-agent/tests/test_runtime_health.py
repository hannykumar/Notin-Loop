from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import runtime.api as api_module
from agent.model_runtime import reset_model_runtime_state
from runtime.runtime_health import runtime_health_snapshot
from integrations.mock_mcp import MockMCP
from runtime.db import init_db, reset_db_state
from runtime.resilience import run_with_timeout
from runtime.settings import reset_settings_cache


def _configure_test_environment(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_health.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("PROMETHEUS_BASE_URL", raising=False)
    monkeypatch.delenv("LOKI_BASE_URL", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("EXECUTOR_BASE_URL", raising=False)
    monkeypatch.delenv("OPS_API_TOKEN", raising=False)
    monkeypatch.setenv("OPS_API_TOKENS", "viewer-token|viewer|Viewer")
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "deterministic")
    reset_model_runtime_state()
    reset_settings_cache()
    reset_db_state()
    init_db()
    MockMCP.reset_live_state()


def _headers(token: str) -> dict[str, str]:
    return {"X-API-Token": token}


def test_runtime_health_endpoint_reports_passive_status(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.get("/platform/runtime-health", headers=_headers("viewer-token"))
    assert response.status_code == 200
    payload = response.json()

    assert payload["planner"]["status"] == "disabled"
    assert payload["live_backends"]["configured"] is False
    assert payload["live_backends"]["components"]["prometheus"]["status"] == "not_configured"
    assert payload["live_backends"]["components"]["kubectl"]["status"] == "not_configured"
    assert payload["summary"]["planner_ready"] is True
    assert payload["summary"]["degraded_components"] == []


def test_runtime_health_reports_degraded_live_component(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("SRE_MCP_BACKEND", "live")
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prometheus.local")
    reset_settings_cache()

    def _fake_probe(active_probe: bool = False):
        return {
            "configured": True,
            "active_probe": active_probe,
            "ready": False,
            "degraded_components": ["prometheus"],
            "components": {
                "prometheus": {"configured": True, "status": "degraded", "detail": "connection refused"},
                "kubectl": {"configured": False, "status": "not_configured", "detail": ""},
                "loki": {"configured": False, "status": "not_configured", "detail": ""},
                "tempo": {"configured": False, "status": "not_configured", "detail": ""},
            },
        }

    monkeypatch.setattr("runtime.runtime_health.probe_live_backend", _fake_probe)
    payload = runtime_health_snapshot(active_probe=True)

    assert payload["live_backends"]["ready"] is False
    assert payload["summary"]["degraded_components"] == ["prometheus"]


def test_runtime_timeout_does_not_wait_for_hung_integration_thread() -> None:
    release = threading.Event()
    started = time.perf_counter()
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            run_with_timeout(lambda: release.wait(1.0), timeout_seconds=0.02)
        assert time.perf_counter() - started < 0.25
    finally:
        release.set()


def test_model_probe_requires_configured_model(monkeypatch) -> None:
    from agent.model_runtime import active_model_status

    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    monkeypatch.setenv("SRE_AGENT_PLANNER_MODEL", "small:1b")
    reset_settings_cache()
    reset_model_runtime_state()
    monkeypatch.setattr("agent.model_runtime._probe_ollama", lambda *args: {"reachable": True, "models": ["other:1b"]})
    assert active_model_status(force=True)["status"] == "missing_model"
    assert active_model_status()["ready"] is False
    monkeypatch.setattr("agent.model_runtime._probe_ollama", lambda *args: {"reachable": True, "models": ["small:1b"]})
    assert active_model_status(force=True)["ready"] is True
    reset_settings_cache()
    reset_model_runtime_state()
