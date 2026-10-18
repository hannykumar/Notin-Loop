from typing import Any, Dict, List

import pytest

import agent.langgraph_agent as lg
import evaluation.runner as eval_lg
from integrations.mock_mcp import MockMCP
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
            "text": "503 responses plus an unavailable payments deployment indicate a dependency outage.",
        }
    ][:limit]


def setup_function() -> None:
    MockMCP.reset_live_state()


@pytest.fixture(autouse=True)
def _use_deterministic_planner(monkeypatch):
    monkeypatch.setenv("SRE_AGENT_PLANNER_PROVIDER", "deterministic")
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_run_incident_plan_only(monkeypatch) -> None:
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_retrieval)
    out = lg.run_incident_langgraph("INC-001", approved=False)

    assert out["incident_id"] == "INC-001"
    assert out["predicted_type"] == "CrashLoopBackOff"
    assert out["planner_backend"] == "deterministic"
    assert out["confirmed"] is True
    assert out["execution_results"] == []
    assert out["requires_human_approval"] is False
    assert out["proposed_action"] == {}
    assert out["approval_prompt"] == ""
    assert out["coordinator_summary"]["summary"]
    assert out["investigation_activity"]
    assert out["infrastructure_memory"]["service"] == "checkout"
    assert "Coordinator summary:" in out["final_response"]
    assert out["incident_brief"]["summary"]
    assert "what_changed" in out["incident_brief"]
    assert out["approval_summary"]["summary"]
    assert out["handoff_summary"]["summary"]
    assert len(out["hypotheses"]) >= 2
    assert "checks" in out["evidence_gate"]
    assert out["claim_validation"]["claims"]
    assert len(out["remediation_options"]) == 1
    assert out["remediation_options"][0]["action"]["action_type"] == "escalate"


def test_run_incident_exec(monkeypatch) -> None:
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_service503_retrieval)
    out = lg.run_incident_langgraph("INC-002", approved=True)

    assert out["execution_results"]
    assert isinstance(out["improved"], bool)
    assert out["saved_trace"]
    assert out["proposed_action"]["action_type"] == "scale_deployment"
    assert out["proposed_action"]["target"] == "payments"
    assert out["execution_results"][0]["status"] == "preview"
    assert out["rca_draft"] == out["final_response"]
    assert out["verification_summary"]["outcome"] == "not_run"


def test_earlier_model_fallback_remains_visible_after_later_success(monkeypatch):
    original = lg.plan_next_step
    calls = []

    def planner(state):
        decision, _, _ = original(state)
        calls.append(True)
        return decision, "ollama_fallback" if len(calls) == 1 else "ollama_guardrail", "first call timed out" if len(calls) == 1 else ""

    monkeypatch.setattr(lg, "plan_next_step", planner)
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_service503_retrieval)
    result = lg.run_incident_langgraph("INC-002", approved=False, tool_mode="mock")
    assert len(calls) > 1
    assert result["planner_backend"] == "ollama_guardrail"
    assert result["planner_error"] == "first call timed out"
    assert result["planner_fallback_count"] == 1


def test_run_incident_preview_generates_commands(monkeypatch) -> None:
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_service503_retrieval)
    out = lg.run_incident_langgraph("INC-002", approved=True, execution_mode="preview")
    assert out["execution_mode"] == "preview"
    assert out["execution_results"]
    assert all(r.get("status") == "preview" for r in out["execution_results"])
    assert out["planned_commands"]


def test_unknown_incident_escalates() -> None:
    out = lg.run_incident_langgraph("INC-004", approved=False)

    assert out["diagnosis"] == "Unknown"
    assert out["confirmed"] is False
    assert out["final_response"] == lg.UNKNOWN_ESCALATION_OUTPUT


def test_run_incident_with_mcp_transport(monkeypatch) -> None:
    pytest.importorskip("mcp")
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_service503_retrieval)
    MockMCP.reset_live_state()

    out = lg.run_incident_langgraph("INC-002", approved=False, tool_mode="mcp")

    assert out["tool_mode"] == "mcp"
    assert out["confirmed"] is True
    assert out["planned_commands"]


def test_evaluation_payload(monkeypatch) -> None:
    monkeypatch.setattr(lg, "retrieve_runbook_chunks", _fake_service503_retrieval)

    payload = eval_lg.run_evaluation(incident_ids=["INC-002"], save_outputs=False)

    assert payload["aggregates"]["n"] == 1.0
    assert payload["rows"][0]["incident"] == "INC-002"
    assert payload["rows"][0]["expected"] == "Service503"
    assert payload["rows"][0]["preview_generated"] is True
    assert payload["rows"][0]["executed"] is False
    assert "verification_success_rate" in payload["aggregates"]
    assert "escalation_quality" in payload["aggregates"]
