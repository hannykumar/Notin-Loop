from __future__ import annotations

from typing import Any, Dict, List

from agent.incident_catalog import incident_definition


def _top_candidate(state: Dict[str, Any]) -> Dict[str, Any]:
    candidates = list(state.get("candidate_diagnoses") or [])
    if not candidates:
        return {}
    return dict(candidates[0] or {})


def _top_supporting_points(state: Dict[str, Any], *, limit: int = 3) -> List[str]:
    candidate = _top_candidate(state)
    points = [str(item).strip() for item in list(candidate.get("supporting_points") or []) if str(item).strip()]
    if len(points) < limit:
        for finding in list(state.get("specialist_findings") or []):
            summary = str((finding or {}).get("summary", "")).strip()
            if summary and summary not in points:
                points.append(summary)
            if len(points) >= limit:
                break
    return points[:limit]


def _top_uncertainties(state: Dict[str, Any], *, limit: int = 3) -> List[str]:
    candidate = _top_candidate(state)
    points = [str(item).strip() for item in list(candidate.get("contradicting_signals") or []) if str(item).strip()]
    coordinator = dict(state.get("coordinator_summary") or {})
    for item in list(coordinator.get("missing_domains") or []):
        text = str(item).strip()
        if text and text not in points:
            points.append(text)
        if len(points) >= limit:
            break
    return points[:limit]


def _top_alternatives(state: Dict[str, Any], *, limit: int = 2) -> List[str]:
    current = str(state.get("diagnosis") or state.get("predicted_type") or "Unknown")
    items: List[str] = []
    for candidate in list(state.get("candidate_diagnoses") or [])[1:]:
        incident_type = str(candidate.get("incident_type", "")).strip()
        if incident_type and incident_type != current:
            items.append(incident_type)
        if len(items) >= limit:
            break
    return items


def _recommended_next_step(state: Dict[str, Any]) -> str:
    if state.get("requires_human_approval"):
        return "Review the proposed action and decide whether to approve it."
    if state.get("next_tool"):
        return f"Collect more evidence with {state.get('next_tool')}."
    if str(state.get("diagnosis", "Unknown")) == "Unknown":
        return "Escalate to a human SRE because the evidence is still weak or conflicting."
    if state.get("execution_results"):
        return "Check verification and decide whether follow-up action is needed."
    return "Review the investigation summary and confirm the next safe step."


def _recent_change_summary(state: Dict[str, Any]) -> str:
    findings = list(state.get("specialist_findings") or [])
    change_finding = next((item for item in findings if str(item.get("specialist")) == "change"), None)
    if change_finding and str(change_finding.get("summary", "")).strip():
        return str(change_finding.get("summary", "")).strip()
    payload = dict((state.get("service_memory") or {}).get("payload") or {})
    deploys = list(payload.get("recent_deploys") or [])
    if deploys:
        latest = deploys[0]
        return f"Latest known deploy {latest.get('version', 'unknown')} {latest.get('minutes_ago', 'n/a')} minutes ago."
    return ""


def build_incident_brief(state: Dict[str, Any]) -> Dict[str, Any]:
    diagnosis = str(state.get("diagnosis") or state.get("predicted_type") or "Unknown")
    confidence = float(state.get("confidence", 0.0) or 0.0)
    incident_id = str(state.get("incident_id", "-"))
    confidence_label = "high" if confidence >= 0.85 else "medium" if confidence >= 0.6 else "low"
    supporting = _top_supporting_points(state)
    uncertainties = _top_uncertainties(state)
    alternatives = _top_alternatives(state)
    if diagnosis == "Unknown":
        summary = "The system could not confirm one clear cause from the current evidence."
    else:
        summary = f"The strongest current explanation is {diagnosis} with {confidence_label} confidence."
    return {
        "headline": f"{incident_id}: {diagnosis}",
        "summary": summary,
        "diagnosis": diagnosis,
        "confidence": round(confidence, 3),
        "what_changed": _recent_change_summary(state),
        "top_evidence": supporting,
        "uncertainties": uncertainties,
        "alternatives": alternatives,
        "recommended_next_step": _recommended_next_step(state),
    }


def build_approval_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    action = dict(state.get("proposed_action") or {})
    if not action:
        return {
            "summary": "No remediation proposal is attached to this run.",
            "risk_level": "none",
            "why_this_action": "",
            "rollback_summary": "",
            "alternatives": [],
        }
    diagnosis = str(state.get("diagnosis", "Unknown"))
    definition = incident_definition(diagnosis)
    action_type = str(action.get("action_type", "none"))
    risk_level = {
        "restart_pod": "low",
        "restart_coredns": "medium",
        "scale_deployment": "medium",
        "gitops_scale_deployment": "medium",
    }.get(action_type, "medium")
    why = str(action.get("reason", "")).strip()
    if not why:
        why = "It is the safest reversible action that matches the current diagnosis."
    alternatives: List[str] = []
    if definition is not None:
        for template in definition.safe_actions:
            candidate = str(template.get("action_type", "")).strip()
            if candidate and candidate != action_type and candidate not in alternatives:
                alternatives.append(candidate)
    rollback_command = ""
    if list(state.get("rollback_commands") or []):
        rollback_command = str((state.get("rollback_commands") or [""])[0]).strip()
    summary = f"Approve {action_type} for {action.get('target', 'the target service')} only if the evidence and rollback path look acceptable."
    return {
        "summary": summary,
        "risk_level": risk_level,
        "why_this_action": why,
        "rollback_summary": rollback_command or "No rollback command was prepared for this action.",
        "alternatives": alternatives[:2],
    }


def build_handoff_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    brief = build_incident_brief(state)
    owner = dict(((state.get("service_memory") or {}).get("payload") or {}).get("owner") or {})
    return {
        "summary": brief.get("summary", ""),
        "top_evidence": list(brief.get("top_evidence") or [])[:3],
        "uncertainties": list(brief.get("uncertainties") or [])[:3],
        "next_step": brief.get("recommended_next_step", ""),
        "owner_team": owner.get("team", ""),
    }


def build_verification_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    verification = dict(state.get("verification") or {})
    improved = bool(state.get("improved", False))
    if state.get("execution_mode") == "preview":
        return {
            "outcome": "not_run",
            "summary": "Preview only. No action or recovery measurement was performed.",
            "next_step": "Review the proposal before choosing simulation or live execution.",
        }
    if not state.get("execution_results"):
        return {
            "outcome": "not_run",
            "summary": "Verification has not started because no action has been executed yet.",
            "next_step": "Approve the remediation first if you want to execute it.",
        }
    status = verification.get("status")
    if status in {"inconclusive", "verification_timeout", "regressed"}:
        return {
            "outcome": status,
            "summary": {
                "inconclusive": "There is insufficient evidence to determine recovery.",
                "verification_timeout": "The verification window timed out; recovery is unconfirmed.",
                "regressed": "The monitored signals worsened after the action.",
            }[status],
            "next_step": "Escalate and inspect current telemetry before taking another action.",
        }
    if verification.get("resolved"):
        return {
            "outcome": "resolved",
            "summary": "The configured recovery checks passed. Review their thresholds before closing the incident.",
            "next_step": "Close the loop and record the outcome for handoff or review.",
        }
    if improved:
        return {
            "outcome": "improved",
            "summary": "The action helped, but the signal has not fully returned to normal.",
            "next_step": "Review remaining evidence and decide whether another safe step or escalation is needed.",
        }
    return {
        "outcome": "unchanged",
        "summary": "The action completed, but the monitored signal did not improve enough.",
        "next_step": "Escalate or collect more evidence before trying another action.",
    }
