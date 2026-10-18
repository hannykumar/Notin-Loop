from __future__ import annotations

import time
from typing import Any, Dict, List

from executor.gitops import GitOpsExecutor
from agent.incident_catalog import incident_definition
from integrations.actions import normalize_action, render_action_to_command, rollback_action_for_action, rollback_command_for_action
from integrations.kubectl_client import KubectlMCPClient
from integrations.tool_gateway import get_tool_client
from runtime.settings import get_settings


def assess_improvement(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    before_metrics = before.get("metrics", {})
    after_metrics = after.get("metrics", {})
    before_logs = "\n".join(before.get("logs_tail", [])).lower()
    after_logs = "\n".join(after.get("logs_tail", [])).lower()

    improved = False
    notes: List[str] = []

    for key in ["error_rate_percent", "dns_error_rate_percent", "memory_percent", "p95_latency_ms"]:
        before_value = before_metrics.get(key)
        after_value = after_metrics.get(key)
        if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
            if after_value < before_value:
                improved = True
            notes.append(f"{key}: {before_value} -> {after_value}")

    tokens = ["503", "nxdomain", "could not resolve", "out of memory", "oom"]
    before_hits = sum(token in before_logs for token in tokens)
    after_hits = sum(token in after_logs for token in tokens)
    if after_hits < before_hits:
        improved = True
    notes.append(f"log_error_signals: {before_hits} -> {after_hits}")

    return {"improved": improved, "summary": "; ".join(notes)}


def assess_verification_rules(diagnosis: str, before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    definition = incident_definition(diagnosis)
    if definition is None:
        return {
            "enabled": False,
            "resolved": False,
            "status": "inconclusive",
            "reason": "no_catalog_verification_rules",
            "checks": [],
        }

    before_metrics = before.get("metrics", {})
    after_metrics = after.get("metrics", {})
    after_logs = "\n".join(after.get("logs_tail", [])).lower()
    checks: List[Dict[str, Any]] = []
    passed_required = True

    for rule in definition.verification_rules:
        if "metric" in rule:
            metric = str(rule["metric"])
            direction = str(rule.get("direction", "decrease"))
            before_value = before_metrics.get(metric)
            after_value = after_metrics.get(metric)
            passed = False
            if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                passed = after_value < before_value if direction == "decrease" else after_value > before_value
                if rule.get("max_value") is not None:
                    passed = passed and after_value <= float(rule["max_value"])
                if rule.get("min_value") is not None:
                    passed = passed and after_value >= float(rule["min_value"])
            checks.append(
                {
                    "type": "metric",
                    "metric": metric,
                    "direction": direction,
                    "before": before_value,
                    "after": after_value,
                    "max_value": rule.get("max_value"),
                    "min_value": rule.get("min_value"),
                    "passed": passed,
                    "optional": bool(rule.get("optional", False)),
                }
            )
        elif "log_absent" in rule:
            tokens = [str(token).lower() for token in rule.get("log_absent", [])]
            passed = all(token not in after_logs for token in tokens)
            checks.append(
                {
                    "type": "log_absent",
                    "tokens": tokens,
                    "passed": passed,
                    "optional": bool(rule.get("optional", False)),
                }
            )
        else:
            continue

        if not checks[-1]["optional"] and not checks[-1]["passed"]:
            passed_required = False

    recovery_checks = [check for check in checks if not check.get("optional") and (
        check["type"] == "log_absent" or check.get("max_value") is not None or check.get("min_value") is not None
    )]
    resolved = bool(recovery_checks) and passed_required
    passed_count = sum(1 for check in checks if check.get("passed"))
    failed_count = sum(1 for check in checks if not check.get("passed"))
    if resolved:
        status = "resolved"
    elif checks and passed_count:
        status = "improved"
    elif checks and failed_count:
        status = "unchanged"
    else:
        status = "inconclusive"
    return {"enabled": bool(checks), "resolved": resolved, "status": status, "reason": diagnosis, "checks": checks}


def classify_verification_outcome(
    before: Dict[str, Any],
    after: Dict[str, Any],
    rule_verification: Dict[str, Any],
    *,
    timed_out: bool = False,
) -> str:
    if timed_out:
        return "verification_timeout"
    if rule_verification.get("resolved"):
        return "resolved"

    before_metrics = dict(before.get("metrics") or {})
    after_metrics = dict(after.get("metrics") or {})
    comparable = 0
    improved = 0
    regressed = 0
    for key in ["error_rate_percent", "dns_error_rate_percent", "memory_percent", "p95_latency_ms"]:
        before_value = before_metrics.get(key)
        after_value = after_metrics.get(key)
        if not isinstance(before_value, (int, float)) or not isinstance(after_value, (int, float)):
            continue
        comparable += 1
        tolerance = max(abs(float(before_value)) * 0.1, 0.1)
        delta = float(after_value) - float(before_value)
        if delta > tolerance:
            regressed += 1
        elif delta < -tolerance:
            improved += 1

    if regressed and regressed >= improved:
        return "regressed"
    if improved:
        return "improved"
    if comparable:
        return "unchanged"
    return "inconclusive"


def collect_stabilized_evidence(
    tool_mode: str,
    incident_id: str,
    incident: Dict[str, Any],
) -> tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
    settings = get_settings()
    duration = max(float(settings.verification_stabilization_seconds), 0.0)
    interval = max(float(settings.verification_sample_interval_seconds), 0.1)
    deadline = time.monotonic() + duration
    samples: List[Dict[str, Any]] = []
    timed_out = False

    while True:
        try:
            sample = collect_evidence(tool_mode, incident_id, incident)
            samples.append({"observed_at": time.time(), "evidence": sample})
        except TimeoutError:
            timed_out = True
            break
        if duration <= 0 or time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    after = dict((samples[-1] if samples else {}).get("evidence") or {})
    return after, samples, timed_out


def collect_evidence(tool_mode: str, incident_id: str, incident: Dict[str, Any]) -> Dict[str, Any]:
    read_client = get_tool_client(tool_mode, incident_id, allow_write=False)
    pod_status = read_client.call_tool("get_pod_status", service=incident["service"], namespace=incident["namespace"])
    return {
        "pod_status": pod_status,
        "pod_describe": read_client.call_tool(
            "describe_pod",
            pod_name=pod_status["pod_name"],
            namespace=incident["namespace"],
        ),
        "metrics": read_client.call_tool("get_metrics", service=incident["service"], namespace=incident["namespace"]),
        "logs_tail": read_client.call_tool(
            "get_pod_logs",
            pod_name=pod_status["pod_name"],
            namespace=incident["namespace"],
            lines=200,
        )[-10:],
        "cluster_events": read_client.call_tool("get_cluster_events", namespace=incident["namespace"]),
    }


def execute_action(
    *,
    run_id: str,
    execution_mode: str,
    tool_mode: str,
    incident_id: str,
    incident: Dict[str, Any],
    action: Dict[str, Any],
    evidence_before: Dict[str, Any],
    diagnosis: str = "Unknown",
) -> Dict[str, Any]:
    normalized_action = normalize_action(action, incident)
    planned_command = render_action_to_command(normalized_action, incident)
    if execution_mode == "preview":
        return {"status": "preview", "execution_results": [], "evidence_after": {}, "improved": False,
                "improvement_summary": "Preview only. No changes were applied.", "command": planned_command, "verification": {}}
    if execution_mode not in {"live", "simulate"}:
        raise ValueError("Execution mode must be preview, simulate, or live")
    if execution_mode == "simulate" and get_settings().mcp_backend != "mock":
        raise ValueError("Simulation requires the mock backend")
    if normalized_action["action_type"].startswith("gitops_") and execution_mode == "live":
        result = GitOpsExecutor().apply_action(run_id=run_id or incident_id, action=normalized_action, incident=incident)
        return {
            "status": "completed",
            "execution_results": [result],
            "evidence_after": {},
            "improved": False,
            "improvement_summary": "GitOps change artifact was created. Merge and rollout happen outside the agent.",
            "command": planned_command,
            "verification": {
                "enabled": False,
                "resolved": False,
                "status": "inconclusive",
                "reason": "gitops_change_created_rollout_not_observed",
                "checks": [],
                "stabilization": {"window_seconds": 0, "sample_count": 0, "samples": []},
            },
        }

    if execution_mode == "live":
        client = KubectlMCPClient()
        result = client.execute_remediation(action=normalized_action, incident=incident)
        after, verification_samples, verification_timed_out = collect_stabilized_evidence(tool_mode, incident_id, incident)
        verification = assess_verification_rules(diagnosis, evidence_before, after)
        verification["status"] = classify_verification_outcome(
            evidence_before,
            after,
            verification,
            timed_out=verification_timed_out,
        )
        verification["stabilization"] = {
            "window_seconds": get_settings().verification_stabilization_seconds,
            "sample_interval_seconds": get_settings().verification_sample_interval_seconds,
            "sample_count": len(verification_samples),
            "samples": verification_samples,
        }
        improvement = assess_improvement(evidence_before, after)
        return {
            "status": "completed",
            "execution_results": [result],
            "evidence_after": after,
            "improved": bool(improvement["improved"] or verification.get("resolved", False)),
            "improvement_summary": improvement["summary"],
            "command": planned_command,
            "verification": verification,
        }

    client = get_tool_client(tool_mode, incident_id, allow_write=True)
    result = client.execute_remediation(action=normalized_action)
    after, verification_samples, verification_timed_out = collect_stabilized_evidence(tool_mode, incident_id, incident)
    improvement = assess_improvement(evidence_before, after)
    verification = assess_verification_rules(diagnosis, evidence_before, after)
    verification["status"] = classify_verification_outcome(
        evidence_before,
        after,
        verification,
        timed_out=verification_timed_out,
    )
    verification["stabilization"] = {
        "window_seconds": get_settings().verification_stabilization_seconds,
        "sample_interval_seconds": get_settings().verification_sample_interval_seconds,
        "sample_count": len(verification_samples),
        "samples": verification_samples,
    }
    return {
        "status": "completed",
        "execution_results": [result],
        "evidence_after": after,
        "improved": bool(improvement["improved"] or verification.get("resolved", False)),
        "improvement_summary": improvement["summary"],
        "command": planned_command,
        "verification": verification,
    }


def execute_rollback(
    *,
    run_id: str,
    execution_mode: str,
    tool_mode: str,
    incident_id: str,
    incident: Dict[str, Any],
    action: Dict[str, Any],
) -> Dict[str, Any]:
    if execution_mode == "preview":
        return {"status": "preview", "execution_results": [], "evidence_after": {}, "improved": False,
                "improvement_summary": "Preview only. No rollback was applied.", "command": rollback_command_for_action(action, incident), "verification": {}}
    if execution_mode not in {"live", "simulate"}:
        raise ValueError("Execution mode must be preview, simulate, or live")
    if execution_mode == "simulate" and get_settings().mcp_backend != "mock":
        raise ValueError("Simulation requires the mock backend")
    rollback_action = rollback_action_for_action(action, incident)
    if rollback_action is None:
        return {
            "status": "not_available",
            "execution_results": [],
            "evidence_after": {},
            "improved": False,
            "improvement_summary": "No executable rollback action is available for this remediation.",
            "command": "",
            "verification": {},
        }

    rollback_command = rollback_command_for_action(action, incident)
    if rollback_action["action_type"].startswith("gitops_") and execution_mode == "live":
        result = GitOpsExecutor().apply_rollback(run_id=run_id or incident_id, rollback_action=rollback_action, incident=incident)
        return {
            "status": "completed",
            "execution_results": [result],
            "evidence_after": {},
            "improved": False,
            "improvement_summary": "GitOps rollback artifact was created.",
            "command": rollback_command,
            "verification": {},
        }

    if execution_mode == "live":
        result = KubectlMCPClient().execute_remediation(action=rollback_action, incident=incident)
        return {
            "status": "completed",
            "execution_results": [result],
            "evidence_after": {},
            "improved": False,
            "improvement_summary": "Rollback executed against the live cluster.",
            "command": rollback_command,
            "verification": {},
        }

    client = get_tool_client(tool_mode, incident_id, allow_write=True)
    result = client.execute_remediation(action=rollback_action)
    after = collect_evidence(tool_mode, incident_id, incident)
    return {
        "status": "completed",
        "execution_results": [result],
        "evidence_after": after,
        "improved": False,
        "improvement_summary": "Rollback executed against the simulated environment.",
        "command": rollback_command,
        "verification": {},
    }
