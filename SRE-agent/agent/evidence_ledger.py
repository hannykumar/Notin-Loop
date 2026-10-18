from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List


MAX_EVIDENCE_PAYLOAD_BYTES = 32_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_payload(payload: Any) -> tuple[Any, bool]:
    encoded = json.dumps(payload, default=str, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= MAX_EVIDENCE_PAYLOAD_BYTES:
        return payload, False
    return {
        "truncated": True,
        "preview": encoded[: MAX_EVIDENCE_PAYLOAD_BYTES // 2],
        "original_bytes": len(encoded.encode("utf-8")),
    }, True


def alert_evidence(context: Dict[str, Any]) -> Dict[str, Any]:
    payload, truncated = _bounded_payload(context.get("raw_alert") or context)
    return {
        "evidence_id": "E000",
        "kind": "alert",
        "tool": "alert_webhook",
        "source": str(context.get("source") or "unknown"),
        "observed_at": str(context.get("received_at") or _utc_now()),
        "time_range": {"start": str(context.get("started_at") or ""), "end": ""},
        "freshness": "current",
        "source_reliability": 0.95,
        "status": "ok",
        "query": {},
        "payload": payload,
        "truncated": truncated,
        "error": "",
    }


def tool_evidence(
    *,
    ledger: List[Dict[str, Any]],
    tool_name: str,
    tool_args: Dict[str, Any],
    observation: Any,
    incident_context: Dict[str, Any] | None = None,
    duration_ms: float = 0.0,
) -> Dict[str, Any]:
    payload, truncated = _bounded_payload(observation)
    context = incident_context or {}
    return {
        "evidence_id": f"E{len(ledger):03d}",
        "kind": "tool_observation",
        "tool": tool_name,
        "source": "mcp",
        "observed_at": _utc_now(),
        "time_range": {
            "start": str(context.get("started_at") or ""),
            "end": str(context.get("received_at") or ""),
        },
        "freshness": "current",
        "source_reliability": 0.9,
        "status": "ok",
        "query": dict(tool_args),
        "payload": payload,
        "truncated": truncated,
        "duration_ms": round(duration_ms, 2),
        "error": "",
    }


def hypothesis_snapshot(candidates: List[Dict[str, Any]], evidence_graph: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    known_ids = {str(item.get("evidence_id")) for item in evidence_graph}
    snapshots: List[Dict[str, Any]] = []
    for index, candidate in enumerate(candidates[:4], start=1):
        supporting = [str(item) for item in candidate.get("citations", []) if str(item) in known_ids]
        contradictions = list(candidate.get("contradicting_signals") or [])
        snapshots.append(
            {
                "id": f"H{index}",
                "cause": str(candidate.get("incident_type") or "Unknown"),
                "confidence": round(float(candidate.get("score", 0.0) or 0.0), 3),
                "supporting_evidence_ids": supporting,
                "contradicting_evidence": contradictions,
                "missing_evidence": [] if supporting else ["No direct tool evidence currently supports this hypothesis."],
                "rationale": list(candidate.get("rationale") or []),
            }
        )
    return snapshots


def validated_model_hypotheses(model_hypotheses: List[Any], evidence_graph: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    known_ids = {str(item.get("evidence_id")) for item in evidence_graph}
    validated: List[Dict[str, Any]] = []
    for index, hypothesis in enumerate(model_hypotheses[:4], start=1):
        item = hypothesis.model_dump() if hasattr(hypothesis, "model_dump") else dict(hypothesis)
        validated.append(
            {
                "id": f"H{index}",
                "cause": str(item.get("cause") or "Unknown"),
                "confidence": round(float(item.get("confidence", 0.0) or 0.0), 3),
                "supporting_evidence_ids": [
                    str(evidence_id) for evidence_id in item.get("supporting_evidence_ids", []) if str(evidence_id) in known_ids
                ],
                "contradicting_evidence_ids": [
                    str(evidence_id) for evidence_id in item.get("contradicting_evidence_ids", []) if str(evidence_id) in known_ids
                ],
                "missing_evidence": [str(value) for value in item.get("evidence_needed", [])],
                "source": "model",
            }
        )
    return validated


def evaluate_evidence_gate(
    candidates: List[Dict[str, Any]],
    evidence_graph: List[Dict[str, Any]],
    *,
    minimum_confidence: float = 0.55,
    minimum_margin: float = 0.08,
) -> Dict[str, Any]:
    top = dict(candidates[0] if candidates else {})
    second = dict(candidates[1] if len(candidates) > 1 else {})
    confidence = float(top.get("score", 0.0) or 0.0)
    margin = confidence - float(second.get("score", 0.0) or 0.0)
    graph_by_id = {str(item.get("evidence_id")): item for item in evidence_graph}
    cited_nodes = [graph_by_id[item] for item in top.get("citations", []) if item in graph_by_id]
    independent_sources = {(str(item.get("source_type")), str(item.get("source_name"))) for item in cited_nodes}
    critical_contradictions = [
        str(item)
        for item in top.get("contradicting_signals", [])
        if str(item).lower().startswith(("critical:", "contradicted:", "rules out:"))
    ]
    checks = {
        "confidence": confidence >= minimum_confidence,
        "margin": margin >= minimum_margin,
        "independent_evidence": len(independent_sources) >= 2,
        "no_critical_contradictions": not critical_contradictions,
        "citations_present": bool(cited_nodes),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "confidence": round(confidence, 3),
        "margin": round(margin, 3),
        "independent_source_count": len(independent_sources),
        "critical_contradictions": critical_contradictions,
    }
