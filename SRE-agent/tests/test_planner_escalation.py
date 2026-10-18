import pytest
from agent.planner import AgentDecision, _parse_planner_response


def test_escalation_reuses_model_explanation_without_changing_decision():
    decision = AgentDecision.model_validate({
        "decision": "escalate", "thought_summary": "Dependency evidence is missing.",
        "escalation_reason": None,
    })
    assert decision.escalation_reason == "Dependency evidence is missing."
    assert decision.decision == "escalate"
    assert decision.proposed_action is None


def test_escalation_schema_bounds_repeated_explanations():
    schema = AgentDecision.model_json_schema()["properties"]["escalation_reason"]
    assert any(branch.get("maxLength") == 400 for branch in schema["anyOf"])
    with pytest.raises(ValueError):
        AgentDecision(decision="escalate", thought_summary="Missing evidence", escalation_reason="x" * 401)
    with pytest.raises(ValueError, match="output token budget"):
        _parse_planner_response({"done_reason": "length", "message": {"content": '{"unfinished":'}})
