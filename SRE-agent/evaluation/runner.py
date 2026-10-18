"""Expanded benchmark harness for the MCP-native SRE agent."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent.langgraph_agent import run_incident_langgraph
from evaluation.live_backends import run_live_backend_probe
from evaluation.replays import generate_replayed_incidents
from integrations.incident_registry import incident_ids
from integrations.mock_mcp import MockMCP

OUT_LATEST = Path("eval_results.json")
HISTORY_DIR = Path("eval_history")
INCIDENTS = incident_ids() or ["INC-001", "INC-002", "INC-003", "INC-004"]


def ground_truth_for_incident(incident_id: str, incident_payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = incident_payload or MockMCP(incident_id).data
    expected = data.get("expected_incident_type") or data.get("incident_type")
    if not expected:
        text = " ".join(
            [
                str(data.get("title", "")),
                str(data.get("description", "")),
                " ".join(data.get("logs_tail", []) or []),
            ]
        ).lower()
        if "oom" in text or "out of memory" in text or "crashloop" in text:
            expected = "CrashLoopBackOff"
        elif "503" in text or "upstream timeout" in text:
            expected = "Service503"
        elif "dns" in text or "nxdomain" in text or "could not resolve" in text:
            expected = "DNSFailure"
        else:
            expected = "Unknown"
    return {
        "expected_incident_type": expected,
        "should_confirm": bool(data.get("should_confirm", True)),
        "expected_verification_outcome": str(data.get("expected_verification_outcome", "")).strip().lower(),
    }


def retrieval_hit_at_k(retrieved: List[Dict[str, Any]], expected_type: str, k: int) -> bool:
    return any((c.get("incident_type") == expected_type) for c in retrieved[:k])


def retrieval_precision_at_k(retrieved: List[Dict[str, Any]], expected_type: str, k: int) -> float:
    selected = retrieved[:k]
    if not selected:
        return 0.0
    hits = sum(1 for chunk in selected if chunk.get("incident_type") == expected_type)
    return hits / len(selected)


def compute_aggregates(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    n = len(rows) or 1

    def rate(key: str) -> float:
        return sum(1 for r in rows if r.get(key) is True) / n

    def avg(key: str) -> float:
        return sum(float(r.get(key, 0.0) or 0.0) for r in rows) / n

    executed_rows = [r for r in rows if r.get("executed") is True]
    no_action_rows = [r for r in rows if r.get("executed") is not True]
    action_precision = (
        sum(1 for r in executed_rows if r.get("action_helped") is True) / len(executed_rows)
        if executed_rows
        else 0.0
    )
    no_action_correctness = (
        sum(1 for r in no_action_rows if r.get("no_action_correct") is True) / len(no_action_rows)
        if no_action_rows
        else 0.0
    )

    return {
        "type_accuracy": rate("type_match"),
        "retrieval_hit@3": rate("hit@3"),
        "retrieval_hit@5": rate("hit@5"),
        "retrieval_precision@5": avg("precision@5"),
        "retrieval_recall@5": rate("hit@5"),
        "verification_pass": rate("verification_pass"),
        "policy_ok_rate": rate("policy_ok"),
        "executed_rate": rate("executed"),
        "improved_rate": rate("improved"),
        "action_precision": action_precision,
        "no_action_correctness": no_action_correctness,
        "unsafe_execution_rate": rate("unsafe_execution"),
        "human_override_rate": rate("human_override_required"),
        "escalation_quality": rate("escalation_correct"),
        "verification_success_rate": rate("verification_success"),
        "avg_retrieval_quality": avg("retrieval_quality_overall"),
        "avg_planner_ms": avg("planner_ms"),
        "avg_retrieval_ms": avg("retrieval_ms"),
        "avg_evidence_coverage": avg("evidence_coverage"),
        "avg_groundedness": avg("groundedness"),
        "avg_tool_calls": avg("tool_calls"),
        "evidence_gate_pass_rate": rate("evidence_gate_passed"),
        "approval_plan_consistency": rate("approval_plan_consistent"),
        "unsupported_claim_rate": avg("unsupported_claim_rate"),
        "n": float(len(rows)),
    }


def evaluate_incident(
    incident_id: str,
    tool_mode: str,
    execution_mode: str,
    incident_payload: Dict[str, Any] | None = None,
    evaluation_profile: str = "hybrid_full",
    run_execution_pass: bool = True,
) -> Dict[str, Any]:
    if incident_payload is None:
        MockMCP.reset_live_state(incident_id)
    gt = ground_truth_for_incident(incident_id, incident_payload=incident_payload)
    expected_type = gt["expected_incident_type"]
    should_confirm = gt["should_confirm"]
    expected_verification_outcome = gt["expected_verification_outcome"] or ("not_run" if execution_mode == "preview" else "")
    context = None
    if incident_payload is not None:
        context = {
            "incident_id": incident_id,
            "source": "evaluation_replay",
            "alert_name": str(incident_payload.get("title") or incident_id),
            "status": "firing",
            "service": str(incident_payload.get("service") or "unknown"),
            "namespace": str(incident_payload.get("namespace") or "default"),
            "environment": "evaluation",
            "severity": str(incident_payload.get("severity") or "unknown"),
            "started_at": "",
            "received_at": "",
            "summary": str(incident_payload.get("description") or "Replay alert"),
            "labels": {},
            "annotations": {},
            "raw_alert": {"replay_id": incident_id, "evaluation_tags": incident_payload.get("evaluation_tags", [])},
        }

    plan_run = run_incident_langgraph(
        incident_id,
        approved=False,
        tool_mode=tool_mode,
        execution_mode=execution_mode,
        incident_payload=incident_payload,
        incident_context=context,
        evaluation_profile=evaluation_profile,
    )

    predicted_type = plan_run.get("diagnosis", plan_run.get("predicted_type", "Unknown"))
    confirmed = bool(plan_run.get("confirmed", False))
    retrieved = plan_run.get("retrieved", [])
    plan = plan_run.get("plan", [])
    policy_ok = bool(plan_run.get("policy_ok", False))
    retrieval_quality = plan_run.get("retrieval_quality", {})
    latency_metrics = plan_run.get("latency_metrics", {})
    claim_validation = dict(plan_run.get("claim_validation") or {})
    claims = list(claim_validation.get("claims") or [])
    evidence_ledger = list(plan_run.get("evidence_ledger") or [])

    row: Dict[str, Any] = {
        "incident": incident_id,
        "evaluation_profile": evaluation_profile,
        "planner_backend": plan_run.get("planner_backend", ""),
        "planner_error": plan_run.get("planner_error", ""),
        "planner_fallback_count": plan_run.get("planner_fallback_count", 0),
        "expected": expected_type,
        "predicted": predicted_type,
        "type_match": predicted_type == expected_type,
        "hit@3": retrieval_hit_at_k(retrieved, expected_type, 3),
        "hit@5": retrieval_hit_at_k(retrieved, expected_type, 5),
        "precision@5": retrieval_precision_at_k(retrieved, expected_type, 5),
        "confirmed": confirmed,
        "should_confirm": should_confirm,
        "verification_pass": confirmed == should_confirm,
        "policy_ok": policy_ok,
        "plan_steps": len(plan),
        "executed": False,
        "preview_generated": False,
        "improved": False,
        "action_needed": should_confirm,
        "action_helped": False,
        "no_action_correct": False,
        "unsafe_execution": False,
        "human_override_required": False,
        "escalation_correct": False,
        "retrieval_quality_overall": float(retrieval_quality.get("overall", 0.0) or 0.0),
        "planner_ms": float(latency_metrics.get("planner_ms", 0.0) or 0.0),
        "retrieval_ms": float(latency_metrics.get("retrieval_ms", 0.0) or 0.0),
        "tool_calls": max(sum(1 for item in evidence_ledger if item.get("kind") == "tool_observation"), 0),
        "evidence_coverage": min(len({item.get("tool") for item in evidence_ledger if item.get("kind") == "tool_observation"}) / 3.0, 1.0),
        "groundedness": float(claim_validation.get("groundedness", 0.0) or 0.0),
        "unsupported_claim_rate": (sum(1 for claim in claims if claim.get("status") == "unsupported") / len(claims)) if claims else 0.0,
        "evidence_gate_passed": bool((plan_run.get("evidence_gate") or {}).get("passed")),
    }
    row["escalation_correct"] = bool(predicted_type == "Unknown" and not should_confirm)

    exec_run = (
        run_incident_langgraph(
            incident_id,
            approved=True,
            tool_mode=tool_mode,
            execution_mode=execution_mode,
            incident_payload=incident_payload,
            incident_context=context,
            evaluation_profile=evaluation_profile,
        )
        if run_execution_pass
        else plan_run
    )
    execution_results = exec_run.get("execution_results", [])
    preview_generated = any(result.get("status") == "preview" for result in execution_results)
    executed = any(result.get("status") == "ok" for result in execution_results) or any(result.get("status") == "completed" for result in execution_results)
    row["executed"] = executed
    row["preview_generated"] = preview_generated
    row["improved"] = bool(exec_run.get("improved", False))
    row["action_helped"] = bool(row["executed"] and exec_run.get("improved", False))
    row["no_action_correct"] = bool((not row["executed"]) and (not should_confirm))
    row["unsafe_execution"] = bool(row["executed"] and not policy_ok)
    row["human_override_required"] = bool(plan_run.get("requires_human_approval") and (predicted_type != expected_type or not should_confirm))
    row["verification_success"] = bool((exec_run.get("verification") or {}).get("resolved", False))
    row["verification_outcome"] = str(exec_run.get("verification_outcome", "not_run"))
    row["verification_matches_expectation"] = bool(
        not expected_verification_outcome or row["verification_outcome"] == expected_verification_outcome
    )
    row["approval_plan_consistent"] = bool(
        not run_execution_pass
        or not plan_run.get("proposed_action")
        or plan_run.get("proposed_action") == exec_run.get("proposed_action")
    )
    row["diagnosis_candidates"] = plan_run.get("candidate_diagnoses", [])[:3]
    row["citations"] = [candidate.get("citations", []) for candidate in plan_run.get("candidate_diagnoses", [])[:1]]
    row["_artifacts"] = {
        "plan_run": {
            "plan": plan_run.get("plan", []),
            "sanitized_plan": plan_run.get("sanitized_plan", []),
            "policy_violations": plan_run.get("policy_violations", []),
            "top_chunks": plan_run.get("top_chunks", []),
            "retrieval_quality": plan_run.get("retrieval_quality", {}),
            "candidate_diagnoses": plan_run.get("candidate_diagnoses", []),
            "saved_trace": plan_run.get("saved_trace", ""),
        },
        "exec_run": {
            "execution_results": exec_run.get("execution_results", []),
            "verification": exec_run.get("verification", {}),
            "improved": exec_run.get("improved", False),
            "improvement_summary": exec_run.get("improvement_summary", ""),
            "saved_trace": exec_run.get("saved_trace", ""),
        },
    }
    return row


def save_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    timestamp = str(payload["timestamp"])
    OUT_LATEST.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = HISTORY_DIR / f"basic_{timestamp}.json"
    snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "latest_path": str(OUT_LATEST.resolve()),
        "history_path": str(snapshot_path.resolve()),
    }


def run_evaluation(
    incident_ids: List[str] | None = None,
    tool_mode: str = "mcp",
    execution_mode: str = "preview",
    save_outputs: bool = True,
    use_replays: bool = True,
    evaluation_profile: str = "hybrid_full",
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    effective_use_replays = bool(use_replays and not incident_ids)
    if effective_use_replays:
        for incident_payload in generate_replayed_incidents():
            rows.append(
                evaluate_incident(
                    str(incident_payload["incident_id"]),
                    tool_mode=tool_mode,
                    execution_mode=execution_mode,
                    incident_payload=incident_payload,
                    evaluation_profile=evaluation_profile,
                )
            )
    else:
        for incident_id in (incident_ids or INCIDENTS):
            rows.append(
                evaluate_incident(
                    incident_id,
                    tool_mode=tool_mode,
                    execution_mode=execution_mode,
                    evaluation_profile=evaluation_profile,
                )
            )

    payload: Dict[str, Any] = {
        "timestamp": datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        "tool_mode": tool_mode,
        "execution_mode": execution_mode,
        "use_replays": effective_use_replays,
        "evaluation_profile": evaluation_profile,
        "aggregates": compute_aggregates(rows),
        "rows": rows,
    }

    if save_outputs:
        payload.update(save_payload(payload))

    return payload


def run_live_observability_evaluation(service: str = "api", namespace: str = "prod") -> Dict[str, Any]:
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        "mode": "live_observability_probe",
        "probe": run_live_backend_probe(service=service, namespace=namespace),
    }


def print_table(rows: List[Dict[str, Any]]) -> None:
    headers = [
        "incident",
        "expected",
        "predicted",
        "type_match",
        "hit@3",
        "hit@5",
        "precision@5",
        "confirmed",
        "should_confirm",
        "verification_pass",
        "policy_ok",
        "plan_steps",
        "executed",
        "improved",
        "human_override_required",
        "unsafe_execution",
    ]
    widths = {h: max(len(h), *(len(str(r.get(h, ""))) for r in rows)) for h in headers}

    def fmt_row(r: Dict[str, Any]) -> str:
        return " | ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers)

    print(fmt_row({h: h for h in headers}))
    print("-" * (sum(widths.values()) + 3 * (len(headers) - 1)))
    for row in rows:
        print(fmt_row(row))


def main() -> None:
    payload = run_evaluation()
    print_table(payload["rows"])
    print("\nAggregate metrics")
    for key, value in payload["aggregates"].items():
        if key == "n":
            print(f"- {key}: {int(value)}")
        else:
            print(f"- {key}: {value:.2f}")

    latest_path = payload.get("latest_path", "")
    history_path = payload.get("history_path", "")
    if latest_path:
        print(f"\nSaved latest:  {latest_path}")
    if history_path:
        print(f"Saved history: {history_path}")


if __name__ == "__main__":
    main()
