from __future__ import annotations

from typing import Any, Dict, List


def _risk_for_action(action_type: str) -> str:
    if action_type in {"restart_pod", "restart_coredns"}:
        return "medium"
    if action_type in {"scale_deployment", "gitops_scale_deployment"}:
        return "medium"
    if "rollback" in action_type:
        return "high"
    return "unknown"


def build_remediation_options(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    action = dict(state.get("proposed_action") or {})
    rollback_commands = list(state.get("rollback_commands") or [])
    if action:
        action_type = str(action.get("action_type") or "unknown")
        options.append(
            {
                "option_id": "R1",
                "recommended": True,
                "execution": "approval_required",
                "action": action,
                "expected_effect": str(action.get("reason") or "Reduce the confirmed incident symptom."),
                "risk": _risk_for_action(action_type),
                "reversible": bool(rollback_commands),
                "preconditions": ["Evidence gate passed", "Proposal is unexpired", "Approver is authorized for the target scope"],
                "verification_plan": ["Check workload readiness", "Compare error rate and latency with the pre-action baseline", "Check alert state"],
                "rollback_plan": rollback_commands,
            }
        )
    options.append(
        {
            "option_id": "R2",
            "recommended": not bool(action),
            "execution": "advisory_only",
            "action": {"action_type": "escalate", "target": "on-call-sre"},
            "expected_effect": "Preserve current state while a human gathers missing or conflicting evidence.",
            "risk": "low",
            "reversible": True,
            "preconditions": [],
            "verification_plan": ["Continue read-only investigation and record newly available evidence"],
            "rollback_plan": [],
        }
    )
    return options


def build_grounded_report(state: Dict[str, Any]) -> Dict[str, Any]:
    graph = list(state.get("evidence_graph") or [])
    graph_by_id = {str(item.get("evidence_id")): item for item in graph}
    known_ids = set(graph_by_id)
    alert_ids = [str(item.get("evidence_id")) for item in graph if item.get("source_type") == "alert"]
    top = dict((state.get("candidate_diagnoses") or [{}])[0])
    diagnosis_ids = [str(item) for item in top.get("citations", []) if str(item) in known_ids]
    diagnosis_sources = {
        (str(graph_by_id[item].get("source_type") or "unknown"), str(graph_by_id[item].get("source_name") or "unknown"))
        for item in diagnosis_ids
    }
    explicit_contradictions = [
        str(item)
        for item in top.get("contradicting_signals", [])
        if str(item).lower().startswith(("critical:", "contradicted:", "rules out:"))
    ]
    diagnosis = str(state.get("diagnosis") or "Unknown")
    confirmed = bool(state.get("confirmed")) and diagnosis != "Unknown"

    claims = [
        {
            "claim_id": "C1",
            "claim": str((state.get("incident") or {}).get("description") or "Incident alert received."),
            "critical": False,
            "evidence_ids": alert_ids,
        },
        {
            "claim_id": "C2",
            "claim": f"The most likely root cause is {diagnosis}.",
            "critical": confirmed,
            "evidence_ids": diagnosis_ids,
            "independent_source_count": len(diagnosis_sources),
        },
    ]
    for claim in claims:
        cited = list(claim["evidence_ids"])
        if claim["claim_id"] == "C2" and explicit_contradictions:
            claim["status"] = "contradicted"
        elif claim["claim_id"] == "C2" and confirmed:
            claim["status"] = "supported" if len(diagnosis_sources) >= 2 else ("partial" if cited else "unsupported")
        else:
            claim["status"] = "supported" if cited else "unsupported"

    supported = sum(1 for claim in claims if claim["status"] == "supported")
    critical_claims = [claim for claim in claims if claim["critical"]]
    critical_supported = bool(critical_claims) and all(claim["status"] == "supported" for claim in critical_claims)
    blocking_claim_ids = [claim["claim_id"] for claim in critical_claims if claim["status"] != "supported"]
    return {
        "incident_report": {
            "summary": str(state.get("situation_summary") or (state.get("incident_brief") or {}).get("summary") or ""),
            "impact": str((state.get("incident") or {}).get("description") or ""),
            "root_cause": diagnosis,
            "confidence": float(state.get("confidence", 0.0) or 0.0),
            "stopping_reason": "evidence_gate_passed" if confirmed else "escalated_with_uncertainty",
            "supporting_evidence_ids": diagnosis_ids,
            "contradictions": list(top.get("contradicting_signals") or []),
            "alternatives": [str(item.get("cause")) for item in list(state.get("hypotheses") or [])[1:3]],
            "unanswered_questions": list((state.get("hypotheses") or [{}])[0].get("missing_evidence") or []),
        },
        "claim_validation": {
            "claims": claims,
            "groundedness": round(supported / len(claims), 3) if claims else 1.0,
            "critical_claims_supported": critical_supported,
            "blocking_claim_ids": blocking_claim_ids,
        },
        "remediation_options": build_remediation_options(state),
    }
