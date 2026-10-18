from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

import agent.langgraph_agent as lg
import runtime.api as api_module
from integrations.mock_mcp import MockMCP
from runtime.db import init_db, reset_db_state
from runtime.settings import reset_settings_cache
from runtime.storage import list_audit_events, mark_run_status


def _fake_retrieval(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    lowered = query.lower()
    if "oom" in lowered or "crashloop" in lowered:
        return [
            {
                "score": 1.0,
                "source_file": "01_crashloopbackoff_oomkilled.md",
                "incident_type": "CrashLoopBackOff",
                "section": "Symptoms",
                "text": "OOMKilled and CrashLoopBackOff observed",
            }
        ][:limit]
    if "dns" in lowered or "resolve" in lowered:
        return [
            {
                "score": 1.0,
                "source_file": "05_dns_resolution_failures.md",
                "incident_type": "DNSFailure",
                "section": "Symptoms",
                "text": "Resolver failures and NXDOMAIN errors indicate DNS issues.",
            }
        ][:limit]
    return [
        {
            "score": 1.0,
            "source_file": "02_service_503_upstream_timeout.md",
            "incident_type": "Service503",
            "section": "Symptoms",
            "text": "503 responses with upstream timeouts indicate dependency failure.",
        }
    ][:limit]


def _configure_test_environment(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "grafana_webhook.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("EXECUTOR_BASE_URL", raising=False)
    monkeypatch.delenv("OPS_API_TOKEN", raising=False)
    monkeypatch.setenv("OPS_API_TOKENS", "viewer-token|viewer|Viewer,operator-token|operator|Operator")
    monkeypatch.setenv("GRAFANA_WEBHOOK_TOKEN", "grafana-test-token")
    monkeypatch.setenv("SRE_WEBHOOK_WAIT_FOR_PLAN", "1")
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "deterministic")
    reset_settings_cache()
    reset_db_state()
    init_db()
    MockMCP.reset_live_state()


def _firing_payload(
    *,
    alertname: str = "Demo API High 5xx Rate",
    summary: str = "Demo API is serving too many 5xx responses.",
    incident_id: str = "INC-002",
    scenario: str = "service503",
    starts_at: str = "2026-03-11T21:00:00Z",
) -> dict[str, Any]:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alertname,
                    "service": "demo-api",
                    "severity": "warning",
                    "incident_id": incident_id,
                    "scenario": scenario,
                },
                "annotations": {
                    "summary": summary,
                },
                "startsAt": starts_at,
                "fingerprint": f"{incident_id}-{scenario}-{starts_at}",
            }
        ],
    }


def test_grafana_webhook_creates_plan_run(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=_firing_payload())
    assert response.status_code == 200
    payload = response.json()

    assert payload["accepted"] is True
    assert payload["created"] is True
    assert payload["incident_id"] == "INC-002"
    assert payload["run"]["request"]["source"] == "grafana_webhook"
    context = payload["run"]["request"]["incident_context"]
    assert context["source"] == "grafana"
    assert context["service"] == "demo-api"
    assert context["summary"] == "Demo API is serving too many 5xx responses."
    assert context["raw_alert"]["alerts"][0]["labels"]["incident_id"] == "INC-002"
    assert payload["run"]["plan_result"]["incident_context"] == context
    assert payload["run"]["plan_result"]["diagnosis"] == "Service503"
    ledger = payload["run"]["plan_result"]["evidence_ledger"]
    assert ledger[0]["kind"] == "alert"
    assert sum(1 for item in ledger if item["kind"] == "tool_observation") >= 2
    assert payload["run"]["plan_result"]["evidence_gate"]["passed"] is True


def test_grafana_webhook_acknowledges_before_investigation(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("SRE_WEBHOOK_WAIT_FOR_PLAN", "0")
    reset_settings_cache()

    api = importlib.reload(api_module)

    class _Queue:
        def submit(self, job_type: str, run_id: str, group_key: str, payload: dict[str, Any]) -> bool:
            assert job_type == "plan"
            assert run_id
            assert group_key
            assert payload["incident_id"] == "INC-002"
            return True

    monkeypatch.setattr(api, "get_job_queue", lambda: _Queue())
    client = TestClient(api.app)
    response = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=_firing_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["created"] is True
    assert payload["run"]["status"] == "queued"
    assert payload["run"]["plan_result"] is None


def test_generic_alert_endpoint_creates_plan_run(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "generic-test-token")
    reset_settings_cache()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.post(
        "/alerts/events?token=generic-test-token",
        json={
            "incident_id": "INC-002",
            "alertname": "Generic Upstream 503 Alert",
            "summary": "API is returning 503 responses from upstream dependencies.",
            "status": "firing",
            "service": "api",
            "severity": "warning",
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["accepted"] is True
    assert payload["created"] is True
    assert payload["normalized"]["source_type"] == "generic"
    assert payload["run"]["request"]["source"] == "generic_alert"


def test_generic_external_incident_without_fixture_is_investigated_safely(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "generic-test-token")
    reset_settings_cache()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)
    raw_alert = {
        "incident_id": "EXT-4711",
        "alertname": "Checkout saturation",
        "summary": "Checkout latency increased after a release.",
        "status": "firing",
        "service": "checkout",
        "namespace": "shop-prod",
        "severity": "critical",
        "environment": "production",
    }

    response = client.post("/alerts/events?token=generic-test-token", json=raw_alert)
    assert response.status_code == 200
    payload = response.json()

    assert payload["incident_id"] == "EXT-4711"
    context = payload["run"]["request"]["incident_context"]
    assert context["service"] == "checkout"
    assert context["namespace"] == "shop-prod"
    assert context["raw_alert"] == raw_alert
    assert payload["run"]["plan_result"]["incident_context"] == context


def test_alert_without_source_incident_id_gets_stable_generated_id(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "generic-test-token")
    reset_settings_cache()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)
    raw_alert = {
        "alertname": "Unfamiliar checkout signal",
        "summary": "A previously unseen checkout condition fired.",
        "status": "firing",
        "service": "checkout",
        "starts_at": "2026-08-22T18:00:00Z",
    }

    first = client.post("/alerts/events?token=generic-test-token", json=raw_alert)
    second = client.post("/alerts/events?token=generic-test-token", json=raw_alert)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["incident_id"].startswith("ALERT-")
    assert first.json()["incident_id"] == second.json()["incident_id"]


def test_grafana_webhook_maps_crashloop_alert(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.post(
        "/alerts/grafana/webhook?token=grafana-test-token",
        json=_firing_payload(
            alertname="Demo API CrashLoop OOM Signal",
            summary="Demo service is emitting a CrashLoop/OOM signal.",
            incident_id="INC-001",
            scenario="crashloop",
        ),
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["incident_id"] == "INC-001"
    assert payload["run"]["plan_result"]["diagnosis"] == "CrashLoopBackOff"


def test_grafana_webhook_maps_dns_alert(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.post(
        "/alerts/grafana/webhook?token=grafana-test-token",
        json=_firing_payload(
            alertname="Demo API DNS Failure Signal",
            summary="Demo service is emitting a DNS failure signal.",
            incident_id="INC-003",
            scenario="dnsfailure",
        ),
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["incident_id"] == "INC-003"
    assert payload["run"]["plan_result"]["diagnosis"] == "DNSFailure"


def test_grafana_webhook_rejects_invalid_token(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.post("/alerts/grafana/webhook?token=wrong", json=_firing_payload())
    assert response.status_code == 401


def test_grafana_webhook_ignores_resolved_alert(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    payload = _firing_payload()
    payload["status"] = "resolved"
    payload["alerts"][0]["status"] = "resolved"
    response = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["created"] is False
    assert body["reason"] == "resolved_alert"


def test_grafana_webhook_does_not_investigate_datasource_meta_alert(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)

    api = importlib.reload(api_module)
    client = TestClient(api.app)
    payload = _firing_payload(alertname="DatasourceError")
    payload["alerts"][0]["annotations"]["Error"] = "Prometheus query endpoint unavailable"

    response = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["created"] is False
    assert body["reason"] == "observability_meta_alert"


def test_grafana_webhook_deduplicates_active_alert(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    first = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=_firing_payload())
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["created"] is True

    second = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=_firing_payload())
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["accepted"] is True
    assert second_body["created"] is False
    assert second_body["reason"] == "duplicate_alert"
    assert second_body["active_run_id"] == first_body["run_id"]


def test_grafana_webhook_creates_new_run_for_new_alert_occurrence(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    first = client.post(
        "/alerts/grafana/webhook?token=grafana-test-token",
        json=_firing_payload(starts_at="2026-03-11T21:00:00Z"),
    )
    assert first.status_code == 200
    first_run = first.json()["run_id"]

    resolved = _firing_payload(starts_at="2026-03-11T21:00:00Z")
    resolved["status"] = "resolved"
    resolved["alerts"][0]["status"] = "resolved"
    resolved_response = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=resolved)
    assert resolved_response.status_code == 200
    assert resolved_response.json()["reason"] == "resolved_alert"
    assert resolved_response.json()["resolved_runs"] == 1

    second = client.post(
        "/alerts/grafana/webhook?token=grafana-test-token",
        json=_firing_payload(starts_at="2026-03-11T21:10:00Z"),
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["created"] is True
    assert second_body["run_id"] != first_run


def test_resolved_webhook_is_audited_after_execution_already_finished(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)
    starts_at = "2026-03-11T21:30:00Z"
    firing = client.post(
        "/alerts/grafana/webhook?token=grafana-test-token",
        json=_firing_payload(starts_at=starts_at),
    )
    run_id = firing.json()["run_id"]
    mark_run_status(run_id, "resolved")

    resolved = _firing_payload(starts_at=starts_at)
    resolved["status"] = "resolved"
    resolved["alerts"][0]["status"] = "resolved"
    response = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=resolved)

    assert response.status_code == 200
    assert response.json()["resolved_runs"] == 1
    events = [event for event in list_audit_events(run_id) if event["event_type"] == "grafana_alert_resolved"]
    assert len(events) == 1

    duplicate = client.post("/alerts/grafana/webhook?token=grafana-test-token", json=resolved)
    assert duplicate.json()["resolved_runs"] == 1
    events = [event for event in list_audit_events(run_id) if event["event_type"] == "grafana_alert_resolved"]
    assert len(events) == 1


def test_plan_run_resets_mock_incident_state(monkeypatch, tmp_path: Path) -> None:
    _configure_test_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    # Simulate a previous remediation mutating the in-memory/file-backed mock state.
    mock = MockMCP("INC-002", allow_write=True)
    mock.restart_pod(service="api", namespace="prod")

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    response = client.post(
        "/runs/plan",
        json={"incident_id": "INC-002", "execution_mode": "preview"},
        headers={"X-API-Token": "operator-token"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["plan_result"]["diagnosis"] == "Service503"
