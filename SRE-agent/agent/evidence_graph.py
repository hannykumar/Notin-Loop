from __future__ import annotations

import itertools
from typing import Any, Dict, List, Tuple

from agent.incident_catalog import all_incident_definitions, incident_definition


def _joined_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def get_path(payload: Dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if part.endswith("]") and "[" in part:
            name, _, raw_index = part[:-1].partition("[")
            if name:
                if not isinstance(current, dict):
                    return None
                current = current.get(name)
            if not isinstance(current, list):
                return None
            try:
                current = current[int(raw_index)]
            except Exception:
                return None
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def evaluate_condition(payload: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    value = get_path(payload, str(condition.get("path", "")))
    if "equals" in condition:
        return value == condition["equals"]
    if "contains" in condition:
        return str(condition["contains"]).lower() in _joined_value(value).lower()
    if "contains_any" in condition:
        joined = _joined_value(value).lower()
        return any(str(token).lower() in joined for token in condition.get("contains_any", []))
    if "gte" in condition:
        try:
            return float(value) >= float(condition["gte"])
        except Exception:
            return False
    if "lte" in condition:
        try:
            return float(value) <= float(condition["lte"])
        except Exception:
            return False
    if "present" in condition:
        return bool(value) is bool(condition["present"])
    return False


def evaluate_rule(payload: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    if rule.get("all"):
        return all(evaluate_condition(payload, condition) for condition in rule.get("all", []))
    if rule.get("any"):
        return any(evaluate_condition(payload, condition) for condition in rule.get("any", []))
    return False


def _graph_source_metadata(source_type: str) -> Dict[str, Any]:
    freshness = {
        "alert": "current",
        "tool": "current",
        "specialist": "derived",
        "runbook": "reference",
        "service_memory": "historical",
    }.get(source_type, "unknown")
    reliability = {
        "alert": 0.95,
        "tool": 0.9,
        "specialist": 0.75,
        "runbook": 0.55,
        "service_memory": 0.65,
    }.get(source_type, 0.5)
    return {"freshness": freshness, "source_reliability": reliability}


def build_evidence_graph(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    counter = itertools.count(1)
    ledger = list(state.get("evidence_ledger") or [])

    incident = state.get("incident", {})
    alert_entries = [item for item in ledger if item.get("kind") == "alert"]
    if alert_entries:
        entry = alert_entries[0]
        nodes.append(
            {
                "evidence_id": str(entry.get("evidence_id") or "E000"),
                "source_type": "alert",
                "evidence_kind": "AlertEvidence",
                "source_name": str(entry.get("source") or "alert_webhook"),
                "summary": str(incident.get("description", "")),
                "path": "incident.description",
                "observed_at": entry.get("observed_at", ""),
                "status": entry.get("status", "ok"),
                "freshness": entry.get("freshness", "current"),
                "source_reliability": entry.get("source_reliability", 0.95),
            }
        )
    elif incident:
        nodes.append(
            {
                "evidence_id": f"A{next(counter)}",
                "source_type": "alert",
                "evidence_kind": "AlertEvidence",
                "source_name": "incident.description",
                "summary": str(incident.get("description", "")),
                "path": "incident.description",
                **_graph_source_metadata("alert"),
            }
        )

    service_memory = dict(state.get("service_memory") or {})
    if service_memory:
        nodes.append(
            {
                "evidence_id": f"S{next(counter)}",
                "source_type": "service_memory",
                "evidence_kind": "ServiceMemoryEvidence",
                "source_name": str(service_memory.get("service", "")),
                "summary": str(service_memory.get("summary", ""))[:280],
                "path": "service_memory",
                **_graph_source_metadata("service_memory"),
            }
        )

    for idx, chunk in enumerate(state.get("top_chunks", [])[:5], start=1):
        nodes.append(
            {
                "evidence_id": f"R{idx}",
                "source_type": "runbook",
                "evidence_kind": "RunbookEvidence",
                "source_name": str(chunk.get("source_file", "")),
                "summary": f"{chunk.get('section', '')}: {str(chunk.get('text', ''))[:220]}",
                "path": f"top_chunks[{idx - 1}]",
                "incident_type": chunk.get("incident_type"),
                **_graph_source_metadata("runbook"),
            }
        )

    tool_entries = [item for item in ledger if item.get("kind") == "tool_observation"]
    if tool_entries:
        for entry in tool_entries:
            tool_name = str(entry.get("tool") or "unknown_tool")
            nodes.append(
                {
                    "evidence_id": str(entry.get("evidence_id")),
                    "source_type": "tool",
                    "evidence_kind": "ToolEvidence",
                    "source_name": tool_name,
                    "summary": _joined_value(entry.get("payload"))[:280],
                    "path": f"tool_results.{tool_name}",
                    "observed_at": entry.get("observed_at", ""),
                    "time_range": entry.get("time_range", {}),
                    "status": entry.get("status", "ok"),
                    "freshness": entry.get("freshness", "current"),
                    "source_reliability": entry.get("source_reliability", 0.9),
                }
            )
    else:
        tool_results = state.get("tool_results", {}) or {}
        for idx, (tool_name, observation) in enumerate(tool_results.items(), start=1):
            nodes.append(
                {
                    "evidence_id": f"T{idx}",
                    "source_type": "tool",
                    "evidence_kind": "ToolEvidence",
                    "source_name": tool_name,
                    "summary": _joined_value(observation)[:280],
                    "path": f"tool_results.{tool_name}",
                    **_graph_source_metadata("tool"),
                }
            )

    for idx, finding in enumerate(state.get("specialist_findings", []) or [], start=1):
        nodes.append(
            {
                "evidence_id": f"F{idx}",
                "source_type": "specialist",
                "evidence_kind": "SpecialistFinding",
                "source_name": str(finding.get("specialist", "")),
                "summary": str(finding.get("summary", ""))[:280],
                "path": f"specialist_findings[{idx - 1}]",
                **_graph_source_metadata("specialist"),
            }
        )

    return nodes


def citation_index(state: Dict[str, Any]) -> Dict[str, List[str]]:
    mapping: Dict[str, List[str]] = {}
    graph = list(state.get("evidence_graph") or [])
    if not graph:
        graph = build_evidence_graph(state)
    for node in graph:
        path = str(node.get("path", ""))
        source_name = str(node.get("source_name", ""))
        evidence_id = str(node.get("evidence_id", ""))
        mapping.setdefault(path, []).append(evidence_id)
        if source_name:
            mapping.setdefault(f"tool:{source_name}", []).append(evidence_id)
            mapping.setdefault(f"runbook:{source_name}", []).append(evidence_id)
            mapping.setdefault(f"specialist:{source_name}", []).append(evidence_id)
    mapping.setdefault("alert:summary", [node["evidence_id"] for node in graph if node.get("source_type") == "alert"])
    return mapping


def rank_diagnoses(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence_payload = {
        "incident": state.get("incident", {}),
        "pod_status": (state.get("evidence") or {}).get("pod_status", {}),
        "pod_describe": (state.get("evidence") or {}).get("pod_describe", {}),
        "metrics": (state.get("evidence") or {}).get("metrics", {}),
        "logs_tail": (state.get("evidence") or {}).get("logs_tail", []),
        "cluster_events": (state.get("evidence") or {}).get("cluster_events", []),
        "prometheus": (state.get("tool_results") or {}).get("query_prometheus", {}),
        "loki": (state.get("tool_results") or {}).get("query_loki", {}),
        "recent_deploys": (state.get("evidence") or {}).get("recent_deploys", []),
        "dashboard_context": (state.get("tool_results") or {}).get("get_dashboard_context", {}),
        "trace_context": (state.get("tool_results") or {}).get("query_tempo", []),
        "service_owner": (state.get("tool_results") or {}).get("get_service_owner", {}),
        "incident_history": (state.get("tool_results") or {}).get("get_incident_history", []),
    }
    retrieval_candidates = {item.get("incident_type"): item for item in state.get("candidate_diagnoses", [])}
    citations = citation_index(state)
    rankings: List[Dict[str, Any]] = []
    specialist_findings = list(state.get("specialist_findings", []) or [])

    for definition in all_incident_definitions():
        matched_rules = []
        strongest_confidence = 0.0
        rationale = []
        rule_citations: List[str] = []
        supporting_points: List[str] = []
        contradicting_points: List[str] = []
        for rule in definition.confirmation_rules:
            if evaluate_rule(evidence_payload, rule):
                matched_rules.append(str(rule.get("rule_id", "rule")))
                strongest_confidence = max(strongest_confidence, float(rule.get("confidence", 0.0)))
                rationale.append(str(rule.get("reason", "")))
                for citation_key in rule.get("citations", []):
                    rule_citations.extend(citations.get(str(citation_key), []))
        for finding in specialist_findings:
            summary = str(finding.get("summary", "")).strip()
            lowered = summary.lower()
            finding_hints = list(finding.get("candidate_hints") or [])
            matching_hint = next(
                (
                    hint
                    for hint in finding_hints
                    if str(hint.get("incident_type", "")).strip() == definition.incident_type
                ),
                None,
            )
            if matching_hint is not None:
                strongest_confidence = max(
                    strongest_confidence,
                    min(float(finding.get("confidence", 0.0) or 0.0) * float(matching_hint.get("weight", 0.0) or 0.0), 0.95),
                )
                supporting_points.append(summary)
                rationale.append(f"{finding.get('specialist', 'specialist')} specialist supports {definition.incident_type.lower()}.")
                for citation_key in finding.get("citations", []):
                    rule_citations.extend(citations.get(str(citation_key), [str(citation_key)]))
            elif any(keyword in lowered for keyword in definition.keywords if keyword):
                supporting_points.append(summary)
                for citation_key in finding.get("citations", []):
                    rule_citations.extend(citations.get(str(citation_key), [str(citation_key)]))
            elif str(finding.get("status", "")).strip() == "missing":
                contradicting_points.append(f"{finding.get('specialist', 'specialist')} evidence is still missing.")
            elif lowered.startswith("no ") or "do not show" in lowered or "not strongly" in lowered:
                contradicting_points.append(summary)
        retrieval_score = float((retrieval_candidates.get(definition.incident_type) or {}).get("retrieval_confidence", 0.0))
        keyword_hits = sum(1 for keyword in definition.keywords if keyword in _joined_value(evidence_payload).lower())
        score = round((strongest_confidence * 0.65) + (retrieval_score * 0.25) + (min(keyword_hits, 4) * 0.025), 3)
        rankings.append(
            {
                "incident_type": definition.incident_type,
                "score": score,
                "matched_rules": matched_rules,
                "retrieval_confidence": retrieval_score,
                "rationale": [item for item in rationale if item],
                "citations": sorted(set(rule_citations)),
                "supporting_evidence": sorted(set(rule_citations)),
                "supporting_points": supporting_points[:3],
                "contradicting_signals": contradicting_points[:2],
            }
        )

    rankings.sort(key=lambda item: item["score"], reverse=True)
    return rankings


def confirmation_from_rankings(rankings: List[Dict[str, Any]]) -> Tuple[str, float, bool]:
    if not rankings:
        return "Unknown", 0.0, False
    top = rankings[0]
    second = rankings[1] if len(rankings) > 1 else {"score": 0.0}
    top_score = float(top.get("score", 0.0))
    margin = top_score - float(second.get("score", 0.0))
    if top_score >= 0.72 and margin >= 0.08:
        return str(top.get("incident_type", "Unknown")), top_score, True
    if top_score >= 0.6 and incident_definition(str(top.get("incident_type", "Unknown"))) is not None:
        return str(top.get("incident_type", "Unknown")), top_score, True
    return "Unknown", min(top_score, 0.2), False
