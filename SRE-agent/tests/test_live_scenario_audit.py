from __future__ import annotations

from copy import deepcopy

from evaluation.live_scenario_audit import SCENARIOS, audit_live_runs


def _run(scenario: str) -> dict:
    contract = SCENARIOS[scenario]
    action_type = contract["action_type"]
    proposal_hash = f"hash-{scenario}" if action_type else ""
    result = {
        "run_id": f"run-{scenario}",
        "request": {"source": "grafana_webhook", "incident_context": {"source": "grafana"}},
        "plan_result": {
            "diagnosis": contract["diagnosis"],
            "confirmed": contract["confirmed"],
            "evidence_gate": {"passed": contract["evidence_gate"]},
            "proposed_action": ({"action_type": action_type, "target": contract.get("target", "")} if action_type else {}),
            "requires_human_approval": bool(action_type),
            "claim_validation": {"groundedness": 1.0},
        },
        "approval": {"artifact": {"proposal_hash": proposal_hash}},
        "execute_result": {
            "executed_proposal_hash": proposal_hash,
            "verification": {
                "status": contract.get("verification", ""),
                "reason": contract.get("verification_reason", ""),
            },
        },
    }
    if scenario == "dependency_503":
        result["plan_result"]["proposed_action"].update({"replicas": 1, "previous_replicas": 0})
        result["approval"]["artifact"]["rollback_plan"] = [contract["rollback_command"]]
        result["plan_result"].update(
            {
                "trace": [
                    {"type": "agent_decision", "hypothesis": "Unknown"},
                    {"type": "tool_observation", "tool_name": "get_metrics"},
                    {"type": "tool_observation", "tool_name": "get_pod_logs"},
                    {"type": "agent_decision", "hypothesis": "Service503"},
                ],
                "hypotheses": [{"cause": "Service503"}, {"cause": "DeploymentRegression"}],
                "claim_validation": {"groundedness": 1.0, "critical_claims_supported": True},
                "remediation_options": [{"option_id": "R1"}, {"option_id": "R2"}],
            }
        )
        result["_audit"] = {"events": [{"event_type": name} for name in contract["audit_events"]]}
    return result


def test_live_scenario_audit_accepts_the_five_contracts() -> None:
    result = audit_live_runs({name: _run(name) for name in SCENARIOS})

    assert result["passed_scenarios"] == 5
    assert result["pass_rate"] == 1.0
    assert result["unsafe_action_count"] == 0


def test_live_scenario_audit_rejects_an_unsafe_ambiguous_action() -> None:
    runs = {name: _run(name) for name in SCENARIOS}
    runs = deepcopy(runs)
    runs["ambiguous"]["plan_result"]["proposed_action"] = {"action_type": "restart_pod"}
    runs["ambiguous"]["plan_result"]["requires_human_approval"] = True

    result = audit_live_runs(runs)

    ambiguous = next(item for item in result["results"] if item["scenario"] == "ambiguous")
    assert ambiguous["passed"] is False
    assert result["unsafe_action_count"] >= 1
