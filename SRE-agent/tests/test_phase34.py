from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from fastapi.testclient import TestClient

import agent.langgraph_agent as lg
import runtime.api as api_module
from agent.grounding import build_grounded_report
from agent.incident_catalog import load_incident_catalog
from agent.deterministic_policy import build_action, ensure_tool_prerequisites, evidence_from_tools, missing_tool, summarize_observation, tool_args_for
from evaluation.replays import generate_replayed_incidents
from executor.engine import assess_verification_rules, classify_verification_outcome, collect_stabilized_evidence, execute_action
from integrations.actions import normalize_action, rollback_action_for_action, rollback_command_for_action
from integrations.live_backends import KubectlLiveAdapter, LiveSREBackend, PrometheusAdapter
from integrations.mcp_client import StdioMCPClient
from integrations.mock_mcp import MockMCP
from integrations.query_safety import validate_limit, validate_time_range
from runtime.db import init_db, reset_db_state
from runtime.settings import reset_settings_cache


def _fake_retrieval(_query: str, limit: int = 8) -> List[Dict[str, Any]]:
    return [
        {
            "score": 1.0,
            "source_file": "01_crashloopbackoff_oomkilled.md",
            "incident_type": "CrashLoopBackOff",
            "section": "Symptoms",
            "text": "OOMKilled and CrashLoopBackOff observed",
        }
    ][:limit]


def _fake_service503_retrieval(_query: str, limit: int = 8) -> List[Dict[str, Any]]:
    return [
        {
            "score": 1.0,
            "source_file": "04_service_503_upstream_down.md",
            "incident_type": "Service503",
            "section": "Symptoms",
            "text": "503 responses plus a dependency with zero ready replicas indicate an upstream outage.",
        }
    ][:limit]


def _configure_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'phase34.db'}")
    monkeypatch.setenv(
        "OPS_API_TOKENS",
        "viewer-token|viewer|Viewer,operator-token|operator|Operator,approver-token|approver|Approver,admin-token|admin|Admin",
    )
    monkeypatch.setenv("GITOPS_REPO_DIR", str(tmp_path / "gitops_repo"))
    monkeypatch.setenv("SRE_MCP_BACKEND", "mock")
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "deterministic")
    monkeypatch.delenv("EXECUTOR_BASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    reset_settings_cache()
    reset_db_state()
    init_db()
    MockMCP.reset_live_state()


def _headers(token: str) -> dict[str, str]:
    return {"X-API-Token": token}


def test_live_kubectl_gateway_joins_kind_network() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.live.yml").read_text(encoding="utf-8")
    kubectl_service = compose.split("  sre-agent:", 1)[0]

    assert "  kubectl-mcp:" in kubectl_service
    assert "networks: [default, kind]" in kubectl_service


def test_alert_scenario_routes_to_discriminating_tools_without_confirming_diagnosis() -> None:
    state = {
        "incident": {
            "service": "demo-api",
            "namespace": "sre-lab",
            "incident_context": {"labels": {"scenario": "deployment_regression"}},
        },
        "predicted_type": "Unknown",
        "candidate_diagnoses": [],
        "tool_results": {},
    }

    assert missing_tool(state) == "get_recent_deploys"


def test_live_log_collection_is_bounded_by_alert_start(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    state = {
        "incident": {"service": "demo-api", "namespace": "sre-lab"},
        "incident_context": {
            "started_at": "2026-08-22T23:00:00Z",
            "received_at": "2026-08-22T23:02:00Z",
        },
        "tool_results": {"get_pod_status": {"pod_name": "demo-api-abc"}},
    }
    args = tool_args_for("get_pod_logs", state)
    captured: list[str] = []
    adapter = KubectlLiveAdapter()

    def fake_run(*command: str) -> str:
        captured.extend(command)
        return "fresh log\n"

    monkeypatch.setattr(adapter, "_run", fake_run)
    logs = adapter.get_pod_logs(
        pod_name=args["pod_name"],
        namespace=args["namespace"],
        lines=args["lines"],
        since_time=args["since_time"],
    )

    assert logs == ["fresh log"]
    assert "--since-time=2026-08-22T23:00:00+00:00" in captured


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_prometheus_adapter_parses_metrics(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prometheus.local")
    reset_settings_cache()

    def fake_urlopen(_url: str, timeout: int = 15):
        return _FakeResponse({"status": "success", "data": {"result": [{"value": [0, "12.5"]}]}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    metrics = PrometheusAdapter().get_metrics(service="checkout", namespace="prod")

    assert metrics["error_rate_percent"] == 12.5
    assert metrics["p95_latency_ms"] == 12.5


def test_prometheus_adapter_uses_safe_configured_request_metric(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prometheus.local")
    monkeypatch.setenv("PROMETHEUS_HTTP_REQUESTS_METRIC", "demo_http_requests_total")
    monkeypatch.setenv("PROMETHEUS_RATE_WINDOW", "1m")
    reset_settings_cache()
    requested_urls: list[str] = []

    def fake_urlopen(url: str, timeout: int = 15):
        requested_urls.append(url)
        return _FakeResponse({"status": "success", "data": {"result": []}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    PrometheusAdapter().get_metrics(service="demo-api", namespace="sre-lab")

    assert any("demo_http_requests_total" in url for url in requested_urls)
    assert any("1m" in url for url in requested_urls)

    monkeypatch.setenv("PROMETHEUS_RATE_WINDOW", "1m) or vector(1)")
    reset_settings_cache()
    import pytest

    with pytest.raises(ValueError, match="PROMETHEUS_RATE_WINDOW"):
        PrometheusAdapter().get_metrics(service="demo-api", namespace="sre-lab")


def test_prometheus_adapter_preserves_partial_metrics(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prometheus.local")
    reset_settings_cache()

    def fake_urlopen(url: str, timeout: int = 15):
        if "http_request_duration_seconds_bucket" in url:
            raise OSError("histogram unavailable")
        return _FakeResponse({"status": "success", "data": {"result": [{"value": [0, "7.5"]}]}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    metrics = PrometheusAdapter().get_metrics(service="checkout", namespace="prod")

    assert metrics["error_rate_percent"] == 7.5
    assert "p95_latency_ms" in metrics["_query_errors"]


def test_dependency_action_requires_live_unavailable_workload(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)

    class _Kubectl:
        def get_deployment(self, service: str, namespace: str) -> Dict[str, Any]:
            assert service == "payments"
            assert namespace == "sre-lab"
            return {"replicas": 0, "ready_replicas": 0}

    dependency_evidence = LiveSREBackend(kubectl=_Kubectl()).get_service_dependencies("demo-api")
    action = build_action(
        {"service": "demo-api", "namespace": "sre-lab"},
        "Service503",
        {"pod_status": {"replicas": 2}, "service_dependencies": dependency_evidence},
    )

    assert action is not None
    assert action["action_type"] == "scale_deployment"
    assert action["target"] == "payments"
    assert action["replicas"] == 1
    assert normalize_action(action, {"service": "demo-api", "namespace": "sre-lab"})["previous_replicas"] == 0
    assert rollback_command_for_action(action, {"service": "demo-api", "namespace": "sre-lab"}) == (
        "kubectl scale deployment payments -n sre-lab --replicas=0"
    )
    assert rollback_action_for_action(action, {"service": "demo-api", "namespace": "sre-lab"})["replicas"] == 0
    assert build_action(
        {"service": "demo-api", "namespace": "sre-lab"},
        "Service503",
        {"pod_status": {"replicas": 2}, "service_dependencies": {"unavailable": [], "unavailable_count": 0}},
    ) is None


def test_gitops_scale_rollback_preserves_zero_replicas() -> None:
    incident = {"service": "payments", "namespace": "sre-lab"}
    action = {
        "action_type": "gitops_scale_deployment",
        "target": "payments",
        "namespace": "sre-lab",
        "replicas": 1,
        "previous_replicas": 0,
        "manifest_path": "clusters/sre-lab/payments.json",
    }

    assert rollback_command_for_action(action, incident) == (
        "gitops update clusters/sre-lab/payments.json set spec.replicas=0 on branch sre-agent/rollback"
    )
    assert rollback_action_for_action(action, incident)["replicas"] == 0


def test_confirmed_oom_has_no_unsafe_restart_or_scale_action() -> None:
    assert build_action(
        {"service": "worker", "namespace": "sre-lab"},
        "CrashLoopBackOff",
        {
            "pod_status": {"restarts": 2, "replicas": 1},
            "pod_describe": {"last_state": {"terminated": {"reason": "OOMKilled"}}},
        },
    ) is None


def test_mcp_error_result_raises_and_non_mapping_evidence_is_shape_safe() -> None:
    result = SimpleNamespace(
        isError=True,
        structuredContent=None,
        content=[SimpleNamespace(text="Error executing tool get_metrics: backend unavailable")],
    )

    try:
        StdioMCPClient()._decode_result(result)
    except RuntimeError as exc:
        assert "backend unavailable" in str(exc)
    else:
        raise AssertionError("MCP error results must raise RuntimeError")

    evidence = evidence_from_tools(
        {
            "incident_context": {"incident_id": "KIND-DEP-503"},
            "tool_results": {"get_metrics": "backend unavailable", "get_pod_logs": "not a list"},
        }
    )
    assert evidence["metrics"] == {}
    assert evidence["logs_tail"] == []
    assert summarize_observation("get_metrics", "backend unavailable") == "backend unavailable"


def test_pod_tools_require_discovery_before_object_level_reads() -> None:
    state: Dict[str, Any] = {"tool_results": {}}
    assert ensure_tool_prerequisites("get_pod_logs", state) == "get_pod_status"
    assert ensure_tool_prerequisites("describe_pod", state) == "get_pod_status"

    state["tool_results"] = {"get_pod_status": {"pod_name": "demo-api-abc"}}
    assert ensure_tool_prerequisites("get_pod_logs", state) == "get_pod_logs"


def test_kubectl_live_adapter_parses_pod_status(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)

    class Completed:
        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd: list[str], check: bool, capture_output: bool, text: bool, timeout: float):
        assert timeout > 0
        if "pods" in cmd:
            return Completed(
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "checkout-abc", "labels": {"replicas": "2"}},
                                "status": {
                                    "phase": "Running",
                                    "containerStatuses": [{"restartCount": 4, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
                                },
                            }
                        ]
                    }
                )
            )
        return Completed(json.dumps({}))

    monkeypatch.setattr("subprocess.run", fake_run)

    payload = KubectlLiveAdapter().get_pod_status(service="checkout", namespace="prod")

    assert payload["pod_name"] == "checkout-abc"
    assert payload["restarts"] == 4
    assert payload["replicas"] == 1


def test_immutable_approval_and_rollback_flow(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_service503_retrieval)

    api = importlib.reload(api_module)
    client = TestClient(api.app)

    plan_response = client.post("/runs/plan", json={"incident_id": "INC-002"}, headers=_headers("operator-token"))
    assert plan_response.status_code == 200
    plan_payload = plan_response.json()
    run_id = plan_payload["run_id"]
    assert plan_payload["plan_result"]["proposed_action"]["action_type"] == "scale_deployment"
    assert plan_payload["plan_result"]["proposed_action"]["target"] == "payments"
    assert plan_payload["plan_result"]["incident_brief"]["summary"]
    assert plan_payload["plan_result"]["approval_summary"]["summary"]
    assert "No remediation proposal" not in plan_payload["plan_result"]["approval_prompt"]
    proposal = plan_payload["approval"]["artifact"]
    assert proposal["proposal_hash"]
    assert proposal["action"] == plan_payload["plan_result"]["proposed_action"]
    assert proposal["evidence_snapshot_hash"]

    approve_response = client.post(f"/runs/{run_id}/approve-execute", headers=_headers("admin-token"))
    assert approve_response.status_code == 200
    executed = approve_response.json()["execute_result"]
    assert executed["executed_proposal_hash"] == proposal["proposal_hash"]
    assert executed["proposed_action"] == proposal["action"]
    feedback_response = client.post(
        f"/runs/{run_id}/feedback",
        json={"rating": 5, "diagnosis_correct": True, "action_helpful": True, "notes": "Matched the observed failure."},
        headers=_headers("operator-token"),
    )
    assert feedback_response.status_code == 200
    assert feedback_response.json()["feedback"]["incident_snapshot"]["executed"] is False
    assert feedback_response.json()["feedback"]["incident_snapshot"]["verification_outcome"] == "not_run"
    memory_response = client.get("/incident-memory", headers=_headers("viewer-token"))
    assert memory_response.status_code == 200
    assert any(item["run_id"] == run_id for item in memory_response.json()["operator_feedback"])
    rollback_response = client.post(f"/runs/{run_id}/rollback-execute", headers=_headers("admin-token"))
    assert rollback_response.status_code == 200
    rollback_payload = rollback_response.json()["rollback_result"]
    assert rollback_payload["status"] == "preview"
    assert rollback_payload["execution_results"] == []
    assert rollback_payload["improvement_summary"]


def test_live_alert_lab_execution_verifies_resolution(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    incident = MockMCP("INC-002").data

    class _Client:
        def execute_remediation(self, action: Dict[str, Any], incident: Dict[str, Any] | None = None) -> Dict[str, Any]:
            return {"status": "ok", "tool": "execute_remediation", "action": action}

    monkeypatch.setattr("executor.engine.KubectlMCPClient", lambda: _Client())
    monkeypatch.setattr(
        "executor.engine.collect_evidence",
        lambda tool_mode, incident_id, incident: {
            "metrics": {"error_rate_percent": 0.0},
            "logs_tail": ["INFO service healthy"],
        },
    )
    out = execute_action(
        run_id="run-live-lab",
        execution_mode="live",
        tool_mode="mcp",
        incident_id="INC-002",
        incident=incident,
        action={
            "action_type": "scale_deployment",
            "target": "payments",
            "namespace": "prod",
            "replicas": 1,
            "previous_replicas": 0,
            "reason": "Restore the confirmed unavailable dependency.",
        },
        evidence_before={
            "metrics": {"error_rate_percent": 18},
            "logs_tail": incident.get("logs_tail", []),
        },
        diagnosis="Service503",
    )

    assert out["improved"] is True
    assert out["verification"]["resolved"] is True
    assert not any(result.get("tool") == "alert_lab_adapter" for result in out["execution_results"])


def test_execute_job_replaces_stale_plan_verification_outcome(monkeypatch) -> None:
    import runtime.job_runner as job_runner

    captured: Dict[str, Any] = {}
    action = {
        "action_type": "scale_deployment",
        "target": "payments",
        "namespace": "sre-lab",
        "replicas": 1,
        "previous_replicas": 0,
    }
    monkeypatch.setattr(job_runner, "mark_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        job_runner,
        "get_run",
        lambda _run_id: {
            "plan_result": {
                "incident": {"service": "demo-api", "namespace": "sre-lab"},
                "verification_outcome": "not_run",
            },
            "approval": {
                "status": "approved",
                "artifact": {"action": action, "diagnosis": "Service503", "proposal_hash": "hash"},
            },
        },
    )
    monkeypatch.setattr(job_runner, "verify_proposal_artifact", lambda *_args: None)

    class _Executor:
        def execute(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "status": "completed",
                "verification": {"status": "resolved", "resolved": True},
                "execution_results": [],
            }

    monkeypatch.setattr(job_runner, "ExecutorClient", lambda: _Executor())
    monkeypatch.setattr(job_runner, "update_execution_result", lambda _run_id, result: captured.update(result))
    monkeypatch.setattr(job_runner, "log_audit_event", lambda *_args, **_kwargs: None)

    job_runner.run_execute_job(
        run_id="run-test",
        incident_id="INC-002",
        execution_mode="live",
        tool_mode="mcp",
        actor="tester",
    )

    assert captured["verification_outcome"] == "resolved"


def test_replay_set_contains_core_and_ambiguous_cases() -> None:
    replay_ids = {item["incident_id"] for item in generate_replayed_incidents()}

    assert any(replay_id.startswith("REPLAY-CL-") for replay_id in replay_ids)
    assert any(replay_id.startswith("REPLAY-503-") for replay_id in replay_ids)
    assert any(replay_id.startswith("REPLAY-DNS-") for replay_id in replay_ids)
    assert any(replay_id.startswith("REPLAY-DEPLOY-") for replay_id in replay_ids)
    assert any(replay_id.startswith("REPLAY-UNK-") for replay_id in replay_ids)
    assert len(replay_ids) == 20


def test_deployment_regression_proposes_exact_gitops_rollback(monkeypatch, tmp_path: Path) -> None:
    _configure_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)

    load_incident_catalog.cache_clear()
    result = lg.run_incident_langgraph("INC-007", approved=False, save_artifacts_enabled=False)

    assert result["diagnosis"] == "DeploymentRegression"
    assert result["proposed_action"]["action_type"] == "gitops_rollback_deployment"
    assert result["proposed_action"]["current_version"] != result["proposed_action"]["previous_version"]


def test_range_query_safety_rejects_oversized_requests() -> None:
    import pytest

    with pytest.raises(ValueError, match="two-hour"):
        validate_time_range("2026-08-22T10:00:00Z", "2026-08-22T13:00:01Z")
    with pytest.raises(ValueError, match="between"):
        validate_limit(501)


def test_claim_grounding_requires_two_independent_sources_and_no_critical_contradiction() -> None:
    base = {
        "incident": {"description": "Checkout errors increased."},
        "diagnosis": "DeploymentRegression",
        "confirmed": True,
        "candidate_diagnoses": [{"incident_type": "DeploymentRegression", "citations": ["E1"]}],
        "evidence_graph": [
            {"evidence_id": "E1", "source_type": "tool", "source_name": "get_recent_deploys"},
            {"evidence_id": "E2", "source_type": "tool", "source_name": "get_metrics"},
        ],
    }
    partial = build_grounded_report(base)["claim_validation"]
    assert partial["critical_claims_supported"] is False
    assert partial["blocking_claim_ids"] == ["C2"]

    supported_state = dict(base)
    supported_state["candidate_diagnoses"] = [
        {"incident_type": "DeploymentRegression", "citations": ["E1", "E2"]}
    ]
    supported = build_grounded_report(supported_state)["claim_validation"]
    assert supported["critical_claims_supported"] is True

    contradicted_state = dict(supported_state)
    contradicted_state["candidate_diagnoses"] = [
        {
            "incident_type": "DeploymentRegression",
            "citations": ["E1", "E2"],
            "contradicting_signals": ["critical: deployment predates the incident"],
        }
    ]
    contradicted = build_grounded_report(contradicted_state)["claim_validation"]
    assert contradicted["critical_claims_supported"] is False
    assert contradicted["claims"][1]["status"] == "contradicted"


def test_verification_outcomes_distinguish_improvement_and_regression() -> None:
    before = {"metrics": {"error_rate_percent": 10.0, "p95_latency_ms": 1000.0}}
    improved = {"metrics": {"error_rate_percent": 4.0, "p95_latency_ms": 700.0}}
    regressed = {"metrics": {"error_rate_percent": 16.0, "p95_latency_ms": 1400.0}}

    assert classify_verification_outcome(before, improved, {"resolved": False}) == "improved"
    assert classify_verification_outcome(before, regressed, {"resolved": False}) == "regressed"
    assert classify_verification_outcome(before, before, {"resolved": False}) == "unchanged"
    assert classify_verification_outcome({}, {}, {"resolved": False}) == "inconclusive"
    assert classify_verification_outcome(before, improved, {"resolved": False}, timed_out=True) == "verification_timeout"


def test_service503_verification_requires_decrease_below_alert_threshold() -> None:
    before = {"metrics": {"error_rate_percent": 30.0}, "logs_tail": ["503 upstream timeout"]}
    still_alerting = {"metrics": {"error_rate_percent": 25.0}, "logs_tail": []}
    recovered = {"metrics": {"error_rate_percent": 4.0}, "logs_tail": []}

    assert assess_verification_rules("Service503", before, still_alerting)["resolved"] is False
    assert assess_verification_rules("Service503", before, recovered)["resolved"] is True


def test_stabilization_window_collects_multiple_samples(monkeypatch) -> None:
    monkeypatch.setenv("SRE_VERIFICATION_STABILIZATION_SECONDS", "0.02")
    monkeypatch.setenv("SRE_VERIFICATION_SAMPLE_INTERVAL_SECONDS", "0.005")
    reset_settings_cache()
    counter = {"value": 0}

    def fake_collect(_tool_mode: str, _incident_id: str, _incident: Dict[str, Any]) -> Dict[str, Any]:
        counter["value"] += 1
        return {"metrics": {"error_rate_percent": float(10 - counter["value"])}}

    monkeypatch.setattr("executor.engine.collect_evidence", fake_collect)
    after, samples, timed_out = collect_stabilized_evidence("direct", "INC-TEST", {"service": "api"})

    assert timed_out is False
    assert len(samples) >= 2
    assert after == samples[-1]["evidence"]
    reset_settings_cache()
