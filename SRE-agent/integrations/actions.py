from __future__ import annotations

from typing import Any, Dict


def normalize_action(action: Dict[str, Any], incident: Dict[str, Any]) -> Dict[str, Any]:
    action_type = str(action.get("action_type", "")).strip()
    if action_type not in {
        "restart_pod",
        "scale_deployment",
        "restart_coredns",
        "gitops_scale_deployment",
        "gitops_rollback_deployment",
    }:
        raise ValueError(f"Unsupported action_type: {action_type}")

    namespace = str(action.get("namespace") or incident.get("namespace") or "default")
    target = str(action.get("target") or incident.get("service") or "").strip()
    reason = str(action.get("reason") or "").strip()

    normalized: Dict[str, Any] = {
        "action_type": action_type,
        "namespace": namespace,
        "target": target,
        "reason": reason,
        "execution_model": str(action.get("execution_model") or ("gitops" if action_type.startswith("gitops_") else "direct")),
    }

    if action_type in {"scale_deployment", "gitops_scale_deployment"}:
        requested_replicas = action.get("replicas")
        normalized["replicas"] = int(2 if requested_replicas is None else requested_replicas)
        previous = action.get("previous_replicas")
        if previous is None:
            previous = incident.get("pod_status", {}).get("replicas", 1)
        normalized["previous_replicas"] = int(1 if previous is None else previous)

    if action_type in {"gitops_scale_deployment", "gitops_rollback_deployment"}:
        normalized["manifest_path"] = str(
            action.get("manifest_path")
            or incident.get("gitops_manifest_path")
            or f"clusters/{namespace}/{target}.json"
        )
    if action_type == "gitops_rollback_deployment":
        normalized["current_version"] = str(action.get("current_version") or incident.get("current_version") or "current")
        normalized["previous_version"] = str(action.get("previous_version") or incident.get("previous_version") or "previous")

    return normalized


def render_action_to_command(action: Dict[str, Any], incident: Dict[str, Any]) -> str:
    action = normalize_action(action, incident)
    action_type = action["action_type"]

    if action_type == "restart_pod":
        return f"kubectl delete pod -n {action['namespace']} -l app={action['target']}"
    if action_type == "scale_deployment":
        return f"kubectl scale deployment {action['target']} -n {action['namespace']} --replicas={action['replicas']}"
    if action_type == "gitops_scale_deployment":
        return (
            f"gitops update {action['manifest_path']} "
            f"set spec.replicas={action['replicas']} on branch sre-agent/change"
        )
    if action_type == "gitops_rollback_deployment":
        return (
            f"gitops update {action['manifest_path']} set image.tag={action['previous_version']} "
            f"on branch sre-agent/rollback-{action['target']}"
        )
    return "kubectl rollout restart deployment/coredns -n kube-system"


def rollback_command_for_action(action: Dict[str, Any], incident: Dict[str, Any]) -> str:
    action = normalize_action(action, incident)
    action_type = action["action_type"]

    if action_type == "restart_pod":
        return f"# rollback: pods recreate automatically for app={action['target']} in ns={action['namespace']}"
    if action_type == "scale_deployment":
        current = int(action["previous_replicas"])
        return f"kubectl scale deployment {action['target']} -n {action['namespace']} --replicas={current}"
    if action_type == "gitops_scale_deployment":
        current = int(action["previous_replicas"])
        return (
            f"gitops update {action['manifest_path']} "
            f"set spec.replicas={current} on branch sre-agent/rollback"
        )
    if action_type == "gitops_rollback_deployment":
        return (
            f"gitops update {action['manifest_path']} set image.tag={action['current_version']} "
            f"on branch sre-agent/revert-rollback-{action['target']}"
        )
    return "# rollback: CoreDNS restart is non-reversible; monitor cluster DNS health"


def rollback_action_for_action(action: Dict[str, Any], incident: Dict[str, Any]) -> Dict[str, Any] | None:
    action = normalize_action(action, incident)
    action_type = action["action_type"]

    if action_type in {"scale_deployment", "gitops_scale_deployment"}:
        rollback_action = dict(action)
        rollback_action["replicas"] = int(action["previous_replicas"])
        rollback_action["reason"] = f"Rollback for {action_type}"
        return rollback_action

    if action_type == "gitops_rollback_deployment":
        rollback_action = dict(action)
        rollback_action["current_version"] = action.get("previous_version", "previous")
        rollback_action["previous_version"] = action.get("current_version", "current")
        rollback_action["reason"] = "Revert the approved deployment rollback"
        return rollback_action

    return None
