from __future__ import annotations

import json
from typing import Any, Dict, List

import agent.langgraph_agent as lg
from agent.model_runtime import reset_model_runtime_state
import agent.planner as planner
import agent.retrieval as retrieval
from integrations.mock_mcp import MockMCP
from runtime.resilience import RetryableOperationError, run_with_retry
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


def _fake_retrieval_for_query(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    lowered = query.lower()
    if "nxdomain" in lowered or "resolve" in lowered or "dns" in lowered:
        return [
            {
                "score": 1.0,
                "source_file": "05_dns_resolution_failures.md",
                "incident_type": "DNSFailure",
                "section": "Symptoms",
                "text": "Resolver failures and NXDOMAIN errors indicate DNS issues.",
            }
        ][:limit]
    if "503" in lowered or "upstream" in lowered:
        return [
            {
                "score": 1.0,
                "source_file": "04_service_503_upstream_down.md",
                "incident_type": "Service503",
                "section": "Symptoms",
                "text": "503 responses with upstream timeouts indicate dependency failure.",
            }
        ][:limit]
    return _fake_retrieval(query, limit)


class _FakeOllamaResponse:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def setup_function() -> None:
    reset_model_runtime_state()
    MockMCP.reset_live_state()


def test_model_cannot_propose_when_deterministic_preconditions_fail(monkeypatch):
    monkeypatch.setattr(planner, "deterministic_decision", lambda state: {
        "thought_summary": "Insufficient evidence", "hypothesis": "Unknown", "confidence": .1,
        "decision": "escalate", "escalation_reason": "Missing prerequisites",
    })
    decision = planner.AgentDecision(thought_summary="Scale anyway", hypothesis="Service503", confidence=.99,
        decision="propose_action", proposed_action=planner.PlannerAction(action_type="scale_deployment", target="payments", replicas=1))
    guarded = planner._apply_deterministic_guardrails({}, decision)
    assert guarded.decision == "escalate"
    assert guarded.proposed_action is None


def test_ollama_planner_path(monkeypatch) -> None:
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    monkeypatch.setenv("SRE_AGENT_PLANNER_MODEL", "qwen3:4b")
    reset_settings_cache()
    MockMCP.reset_live_state()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval_for_query)

    ollama_payload = {
        "message": {
            "content": json.dumps(
                {
                    "thought_summary": "The payments deployment is confirmed unavailable. Restore exactly one replica.",
                    "hypothesis": "Service503",
                    "confidence": 0.94,
                    "decision": "propose_action",
                    "tool_name": None,
                    "proposed_action": {
                        "action_type": "scale_deployment",
                        "target": "payments",
                        "namespace": "prod",
                        "replicas": 1,
                        "previous_replicas": 0,
                        "reason": "Restore the dependency after live workload evidence confirmed zero ready replicas.",
                    },
                    "escalation_reason": None,
                }
            )
        }
    }
    monkeypatch.setattr(planner, "urlopen", lambda req, timeout=20: _FakeOllamaResponse(ollama_payload))

    out = lg.run_incident_langgraph("INC-002", approved=False)

    assert out["planner_backend"] == "ollama"
    assert out["diagnosis"] == "Service503"
    assert out["confirmed"] is True
    assert out["proposed_action"]["action_type"] == "scale_deployment"
    assert out["proposed_action"]["target"] == "payments"
    assert out["planned_commands"]
    reset_settings_cache()


def test_ollama_guardrail_recovers_service503_from_escalation(monkeypatch) -> None:
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    reset_settings_cache()
    MockMCP.reset_live_state()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval_for_query)

    monkeypatch.setattr(
        planner,
        "_call_ollama",
        lambda state: planner.AgentDecision(
            thought_summary="Data is unclear. Escalate.",
            hypothesis="Service503",
            confidence=0.2,
            decision="escalate",
            tool_name=None,
            proposed_action=None,
            escalation_reason="Insufficient evidence.",
        ),
    )

    out = lg.run_incident_langgraph("INC-002", approved=False)

    assert out["planner_backend"] == "ollama_guardrail"
    assert out["diagnosis"] == "Service503"
    assert out["confirmed"] is True
    assert out["proposed_action"]["action_type"] == "scale_deployment"
    assert out["proposed_action"]["target"] == "payments"
    assert out["planned_commands"]
    reset_settings_cache()


def test_ollama_guardrail_overrides_service503_conflicting_action(monkeypatch) -> None:
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    reset_settings_cache()
    MockMCP.reset_live_state()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval_for_query)

    monkeypatch.setattr(
        planner,
        "_call_ollama",
        lambda state: planner.AgentDecision(
            thought_summary="503 evidence is clear. Scale the deployment.",
            hypothesis="Service503",
            confidence=0.93,
            decision="propose_action",
            tool_name=None,
            proposed_action=planner.PlannerAction(
                action_type="scale_deployment",
                target="api",
                namespace="prod",
                replicas=3,
                previous_replicas=1,
                reason="Add capacity after repeated 503s.",
            ),
            escalation_reason=None,
        ),
    )

    out = lg.run_incident_langgraph("INC-002", approved=False)

    assert out["planner_backend"] == "ollama_guardrail"
    assert out["diagnosis"] == "Service503"
    assert out["confirmed"] is True
    assert out["proposed_action"]["action_type"] == "scale_deployment"
    assert out["proposed_action"]["target"] == "payments"
    assert out["proposed_action"]["replicas"] == 1
    assert out["planned_commands"][0] == "kubectl scale deployment payments -n prod --replicas=1"
    reset_settings_cache()


def test_ollama_guardrail_recovers_dnsfailure_from_escalation(monkeypatch) -> None:
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    reset_settings_cache()
    MockMCP.reset_live_state()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval_for_query)

    monkeypatch.setattr(
        planner,
        "_call_ollama",
        lambda state: planner.AgentDecision(
            thought_summary="Signals are inconclusive. Escalate.",
            hypothesis="DNSFailure",
            confidence=0.15,
            decision="escalate",
            tool_name=None,
            proposed_action=None,
            escalation_reason="Not enough evidence.",
        ),
    )

    out = lg.run_incident_langgraph("INC-003", approved=False)

    assert out["planner_backend"] == "ollama_guardrail"
    assert out["diagnosis"] == "DNSFailure"
    assert out["confirmed"] is True
    assert out["proposed_action"] == {}
    assert out["planned_commands"] == []
    assert out["requires_human_approval"] is False
    assert "no preconditioned safe action" in out["escalation_reason"].lower()
    reset_settings_cache()


def test_ollama_failure_enters_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    monkeypatch.setenv("SRE_AGENT_PLANNER_FAILURE_COOLDOWN_SECONDS", "60")
    reset_settings_cache()
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval_for_query)

    def _boom(state):
        raise RuntimeError("ollama unavailable")

    monkeypatch.setattr(planner, "_call_ollama", _boom)

    first = lg.run_incident_langgraph("INC-002", approved=False)
    second = lg.run_incident_langgraph("INC-002", approved=False)

    assert first["planner_backend"] == "ollama_run_fallback"
    assert second["planner_backend"] == "ollama_run_fallback"
    assert second["model_status"]["cooldown_active"] is True
    reset_settings_cache()


def test_retry_query_uses_ai_rewrite_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "ollama")
    monkeypatch.setenv("SRE_AGENT_PLANNER_MODEL", "qwen3:4b")
    reset_settings_cache()

    payload = {
        "message": {
            "content": json.dumps(
                {
                    "query": "api upstream timeout 503 payments dependency latency"
                }
            )
        }
    }
    monkeypatch.setattr(retrieval, "urlopen", lambda req, timeout=8: _FakeOllamaResponse(payload))

    query = retrieval.build_retry_query(
        {"incident_id": "INC-002", "service": "api", "namespace": "prod", "description": "users see 503 errors"},
        {"metrics": {"error_rate_percent": 9}, "logs_tail": ["ERROR upstream timeout to payments"], "cluster_events": []},
        [{"incident_type": "Service503", "score": 0.5}],
    )

    assert query == "api upstream timeout 503 payments dependency latency"
    reset_settings_cache()


def test_retry_error_preserves_underlying_exception_text() -> None:
    def _boom():
        raise TimeoutError("timed out")

    try:
        run_with_retry(
            _boom,
            retries=0,
            timeout_seconds=1.0,
            operation_name="planner:ollama",
            retry_exceptions=(TimeoutError,),
        )
    except RetryableOperationError as exc:
        text = str(exc)
        assert "planner:ollama failed after 1 attempts" in text
        assert "TimeoutError:" in text
        assert "timed out" in text.lower()
    else:
        raise AssertionError("RetryableOperationError was not raised")
