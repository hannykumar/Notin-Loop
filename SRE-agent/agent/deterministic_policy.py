from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from agent.evidence_graph import confirmation_from_rankings, rank_diagnoses
from agent.incident_catalog import allowed_catalog_actions, allowed_catalog_tools, incident_definition
from agent.state import AgentState, StructuredAction


ALLOWED_READ_TOOLS = allowed_catalog_tools()
ALLOWED_ACTION_TYPES = allowed_catalog_actions()


def incident_routing_hint(incident: Dict[str, Any]) -> str:
    """Use provider metadata only to choose evidence tools, never to confirm a diagnosis."""
    context = dict(incident.get("incident_context") or {})
    labels = dict(context.get("labels") or {})
    scenario = str(labels.get("scenario") or "").strip().lower().replace("-", "_")
    scenario_types = {
        "crashloop": "CrashLoopBackOff",
        "oom": "CrashLoopBackOff",
        "service503": "Service503",
        "dependency503": "Service503",
        "deployment_regression": "DeploymentRegression",
        "dnsfailure": "DNSFailure",
        "dns_failure": "DNSFailure",
    }
    hinted = scenario_types.get(scenario, "")
    return hinted if incident_definition(hinted) is not None else ""


def tool_args_for(tool_name: str, state: AgentState) -> Dict[str, Any]:
    incident = state["incident"]
    pod_status = state.get("tool_results", {}).get("get_pod_status", {})
    pod_name = pod_status.get("pod_name") or incident.get("pod_status", {}).get("pod_name")
    service = str(incident.get("service", ""))
    namespace = str(incident.get("namespace", "default"))
    context = dict(state.get("incident_context") or {})
    now = datetime.now(timezone.utc)
    try:
        end_at = datetime.fromisoformat(str(context.get("received_at") or now.isoformat()).replace("Z", "+00:00"))
        start_at = datetime.fromisoformat(
            str(context.get("started_at") or (end_at - timedelta(minutes=15)).isoformat()).replace("Z", "+00:00")
        )
        if start_at >= end_at or end_at - start_at > timedelta(hours=2):
            start_at = end_at - timedelta(minutes=15)
    except ValueError:
        end_at = now
        start_at = now - timedelta(minutes=15)
    start = start_at.astimezone(timezone.utc).isoformat()
    end = end_at.astimezone(timezone.utc).isoformat()

    if tool_name == "get_pod_status":
        return {"service": service, "namespace": namespace}
    if tool_name == "describe_pod":
        return {"pod_name": pod_name, "namespace": namespace}
    if tool_name == "get_metrics":
        return {"service": service, "namespace": namespace}
    if tool_name == "get_pod_logs":
        return {"pod_name": pod_name, "namespace": namespace, "lines": 200, "since_time": start}
    if tool_name == "get_cluster_events":
        return {"namespace": namespace}
    if tool_name == "get_kubernetes_events":
        return {"namespace": namespace}
    if tool_name == "get_deployment":
        return {"service": service, "namespace": namespace}
    if tool_name == "query_prometheus":
        return {"expression": f"service_health_score{{service=\"{service}\",namespace=\"{namespace}\"}}"}
    if tool_name == "query_prometheus_range":
        return {"expression": f"service_health_score{{service=\"{service}\",namespace=\"{namespace}\"}}", "start": start, "end": end, "step_seconds": 30}
    if tool_name == "query_loki":
        return {"service": service, "namespace": namespace, "limit": 50}
    if tool_name == "query_loki_range":
        return {"query": f'{{service="{service}",namespace="{namespace}"}}', "start": start, "end": end, "limit": 50}
    if tool_name == "query_tempo":
        return {"service": service, "namespace": namespace, "limit": 20}
    if tool_name == "get_dashboard_context":
        return {"service": service, "namespace": namespace}
    if tool_name == "get_recent_deploys":
        return {"service": service, "namespace": namespace}
    if tool_name == "get_service_owner":
        return {"service": service}
    if tool_name == "get_incident_history":
        return {"service": service, "limit": 10}
    if tool_name == "get_git_diff":
        return {"service": service, "namespace": namespace}
    if tool_name == "get_service_dependencies":
        return {"service": service, "namespace": namespace}
    if tool_name == "search_runbooks":
        return {"query": str(incident.get("description") or service), "limit": 5}
    if tool_name == "search_previous_incidents":
        return {"service": service, "limit": 10}
    return {"service": service, "namespace": namespace}


def ensure_tool_prerequisites(tool_name: str, state: AgentState) -> str:
    """Route tools with object-level arguments through discovery first."""
    if tool_name in {"get_pod_logs", "describe_pod"}:
        pod_status = state.get("tool_results", {}).get("get_pod_status")
        if not isinstance(pod_status, dict) or not str(pod_status.get("pod_name") or "").strip():
            return "get_pod_status"
    return tool_name


def summarize_observation(tool_name: str, observation: Any) -> str:
    if not isinstance(observation, (dict, list)):
        return str(observation)
    if tool_name == "get_pod_status":
        return f"pod={observation.get('pod_name')} restarts={observation.get('restarts')} phase={observation.get('phase')}"
    if tool_name == "describe_pod":
        reason = observation.get("last_state", {}).get("terminated", {}).get("reason", "unknown")
        return f"last_termination_reason={reason}"
    if tool_name == "get_metrics":
        parts = []
        for key in ["cpu_percent", "memory_percent", "error_rate_percent", "dns_error_rate_percent", "p95_latency_ms"]:
            if key in observation:
                parts.append(f"{key}={observation[key]}")
        return ", ".join(parts) if parts else "metrics collected"
    if tool_name == "get_pod_logs":
        logs = observation[-2:] if isinstance(observation, list) else []
        return " | ".join(logs)
    if tool_name == "get_cluster_events":
        events = observation[-2:] if isinstance(observation, list) else []
        return " | ".join(events)
    if tool_name == "query_prometheus":
        return f"prometheus_query={observation.get('expression', '')} value={observation.get('value', 'n/a')}"
    if tool_name == "query_loki":
        logs = observation.get("lines", [])[-2:] if isinstance(observation, dict) else []
        return " | ".join(logs)
    if tool_name == "query_tempo":
        spans = observation.get("spans", []) if isinstance(observation, dict) else []
        return "; ".join(f"{span.get('span')}:{span.get('error', '')}" for span in spans[:2])
    if tool_name == "get_dashboard_context":
        return f"dashboard={observation.get('dashboard_uid', 'n/a')} panels={len(observation.get('panels', []))}"
    if tool_name == "get_recent_deploys":
        deploys = observation if isinstance(observation, list) else []
        if not deploys:
            return "no recent deploys"
        latest = deploys[0]
        return f"latest_deploy={latest.get('version', 'unknown')} {latest.get('minutes_ago', 'n/a')}m ago"
    if tool_name == "get_service_owner":
        return f"team={observation.get('team', 'unknown')} pagerduty={observation.get('pagerduty', 'n/a')}"
    if tool_name == "get_incident_history":
        incidents = observation if isinstance(observation, list) else []
        if not incidents:
            return "no recent incident history"
        latest = incidents[0]
        return f"history={latest.get('type', 'unknown')} {latest.get('hours_ago', 'n/a')}h ago"
    if tool_name == "get_service_dependencies":
        unavailable = observation.get("unavailable", []) if isinstance(observation, dict) else []
        if unavailable:
            names = ", ".join(str(item.get("name") or "unknown") for item in unavailable if isinstance(item, dict))
            return f"unavailable_dependencies={names}"
        dependencies = observation.get("dependencies", []) if isinstance(observation, dict) else []
        return f"dependencies_checked={len(dependencies)}; none confirmed unavailable"
    return str(observation)


def evidence_from_tools(state: AgentState) -> Dict[str, Any]:
    tools = state.get("tool_results", {})
    # Fixture payloads model the environment behind mock tools. For a real alert
    # they must not count as already-observed evidence before a tool is called.
    embedded = {} if state.get("incident_context") else state.get("incident", {})
    def mapping(value: Any, fallback: Any = None) -> Dict[str, Any]:
        candidate = fallback if value is None else value
        return candidate if isinstance(candidate, dict) else {}

    def sequence(value: Any, fallback: Any = None) -> list[Any]:
        candidate = fallback if value is None else value
        return candidate if isinstance(candidate, list) else []

    return {
        "pod_status": mapping(tools.get("get_pod_status"), embedded.get("pod_status", {})),
        "pod_describe": mapping(tools.get("describe_pod"), embedded.get("pod_describe", {})),
        "metrics": mapping(tools.get("get_metrics"), embedded.get("metrics", {})),
        "logs_tail": sequence(tools.get("get_pod_logs"), embedded.get("logs_tail", [])),
        "cluster_events": sequence(tools.get("get_kubernetes_events", tools.get("get_cluster_events")), embedded.get("cluster_events", [])),
        "deployment": mapping(tools.get("get_deployment")),
        "prometheus": mapping(tools.get("query_prometheus_range", tools.get("query_prometheus"))),
        "loki": mapping(tools.get("query_loki_range", tools.get("query_loki"))),
        "tempo": mapping(tools.get("query_tempo")),
        "dashboard_context": mapping(tools.get("get_dashboard_context")),
        "recent_deploys": sequence(tools.get("get_recent_deploys"), embedded.get("recent_deploys", [])),
        "service_owner": mapping(tools.get("get_service_owner")),
        "incident_history": sequence(tools.get("search_previous_incidents", tools.get("get_incident_history"))),
        "git_diff": mapping(tools.get("get_git_diff")),
        "service_dependencies": mapping(tools.get("get_service_dependencies")),
        "runbook_search": sequence(tools.get("search_runbooks")),
    }


def diagnose_from_evidence(state: AgentState) -> Dict[str, Any]:
    rankings = rank_diagnoses({**state, "evidence": evidence_from_tools(state)})
    diagnosis, confidence, confirmed = confirmation_from_rankings(rankings)
    return {
        "diagnosis": diagnosis,
        "confidence": confidence,
        "confirmed": confirmed,
        "rankings": rankings,
    }


def missing_tool(state: AgentState) -> str:
    seen = state.get("tool_results", {})
    predicted_type = incident_routing_hint(state.get("incident", {})) or state.get("predicted_type", "Unknown")
    definition = incident_definition(predicted_type) if predicted_type != "Unknown" else None
    if definition is None and state.get("candidate_diagnoses"):
        top_candidate = str(state.get("candidate_diagnoses", [{}])[0].get("incident_type", "Unknown"))
        definition = incident_definition(top_candidate)
    sequence = list(definition.discriminating_tools) if definition else [
        "get_pod_status",
        "get_metrics",
        "get_pod_logs",
        "describe_pod",
        "get_cluster_events",
        "get_recent_deploys",
        "get_service_owner",
        "get_incident_history",
    ]
    for tool_name in sequence:
        if tool_name not in seen:
            return tool_name
    return ""


def _condition_matches(incident: Dict[str, Any], evidence: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    path = str(condition.get("path", ""))
    if path.startswith("incident."):
        current: Any = incident
        path = path[len("incident."):]
    else:
        current = {**evidence, "incident": incident}
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    if "equals" in condition:
        return current == condition["equals"]
    if "lte" in condition:
        try:
            return float(current) <= float(condition["lte"])
        except Exception:
            return False
    if "gte" in condition:
        try:
            return float(current) >= float(condition["gte"])
        except Exception:
            return False
    return False


def build_action(incident: Dict[str, Any], diagnosis: str, evidence: Dict[str, Any]) -> StructuredAction | None:
    definition = incident_definition(diagnosis)
    namespace = str(incident.get("namespace") or "default")
    service = str(incident.get("service") or "service")
    replicas = int((evidence.get("pod_status") or {}).get("replicas", incident.get("pod_status", {}).get("replicas", 1)) or 1)

    if definition is None:
        return None

    for template in definition.safe_actions:
        conditions = list(template.get("conditions", []))
        if conditions and not all(_condition_matches(incident, evidence, condition) for condition in conditions):
            continue
        params = dict(template.get("params", {}))
        action_type = str(template.get("action_type", "restart_pod"))
        target = str(template.get("target") or ("coredns" if action_type == "restart_coredns" else service))
        action_namespace = str(template.get("namespace") or ("kube-system" if action_type == "restart_coredns" else namespace))
        previous_replicas = replicas
        if template.get("target_source") == "first_unavailable_dependency":
            unavailable = list((evidence.get("service_dependencies") or {}).get("unavailable") or [])
            candidate = dict(unavailable[0]) if unavailable and isinstance(unavailable[0], dict) else {}
            if type(candidate.get("replicas")) is not int or candidate["replicas"] != 0:
                continue
            target = str(candidate.get("name") or "").strip()
            action_namespace = str(candidate.get("namespace") or namespace)
            previous_replicas = 0
            if not target:
                continue
        action: StructuredAction = {
            "action_type": action_type,
            "target": target,
            "namespace": action_namespace,
            "reason": str(template.get("reason", "Safe catalog action.")),
        }
        if action_type in {"scale_deployment", "gitops_scale_deployment"}:
            action["replicas"] = int(params.get("replicas", max(replicas + 1, 2)))
            action["previous_replicas"] = previous_replicas
        if action_type == "gitops_scale_deployment":
            action["execution_model"] = "gitops"
            action["manifest_path"] = str(incident.get("gitops_manifest_path") or f"clusters/{namespace}/{service}.json")
        if action_type == "gitops_rollback_deployment":
            action["execution_model"] = "gitops"
            action["manifest_path"] = str(incident.get("gitops_manifest_path") or f"clusters/{namespace}/{service}.json")
            action["current_version"] = str(params.get("current_version") or incident.get("current_version") or "current")
            action["previous_version"] = str(params.get("previous_version") or incident.get("previous_version") or "previous")
        return action

    return None


def deterministic_decision(state: AgentState) -> Dict[str, Any]:
    evidence = evidence_from_tools(state)
    diagnosis = diagnose_from_evidence({**state, "evidence": evidence})
    diagnosis_name = diagnosis["diagnosis"]
    confidence = float(diagnosis["confidence"])

    if bool(diagnosis["confirmed"]):
        proposed_action = build_action(state["incident"], diagnosis_name, evidence)
        top_ranking = (diagnosis.get("rankings") or [{}])[0]
        rationale = (
            "; ".join(top_ranking.get("rationale", []))
            or f"Evidence is strong enough to diagnose {diagnosis_name}."
        )[:280]
        if proposed_action is not None:
            return {
                "thought_summary": rationale,
                "hypothesis": diagnosis_name,
                "confidence": confidence,
                "decision": "propose_action",
                "tool_name": None,
                "proposed_action": proposed_action,
                "escalation_reason": None,
            }
        next_tool = missing_tool(state)
        if next_tool and int(state.get("step_count", 0)) <= int(state.get("max_steps", 4)):
            return {
                "thought_summary": f"{rationale} Gather {next_tool} before deciding whether any catalog action is safe.",
                "hypothesis": diagnosis_name,
                "confidence": confidence,
                "decision": "call_tool",
                "tool_name": next_tool,
                "proposed_action": None,
                "escalation_reason": None,
            }
        return {
            "thought_summary": f"{rationale} No catalog action satisfies the observed preconditions.",
            "hypothesis": diagnosis_name,
            "confidence": confidence,
            "decision": "escalate",
            "tool_name": None,
            "proposed_action": None,
            "escalation_reason": f"Diagnosis {diagnosis_name} is supported, but no preconditioned safe action is available.",
        }

    next_tool = missing_tool(state)
    if next_tool and int(state.get("step_count", 0)) <= int(state.get("max_steps", 4)):
        return {
            "thought_summary": f"Current evidence is not enough. Call {next_tool} next to verify the hypothesis.",
            "hypothesis": state.get("predicted_type", "Unknown"),
            "confidence": confidence,
            "decision": "call_tool",
            "tool_name": next_tool,
            "proposed_action": None,
            "escalation_reason": None,
        }

    return {
        "thought_summary": "The evidence is still insufficient after the allowed tool budget. Escalate to a human SRE.",
        "hypothesis": "Unknown",
        "confidence": 0.2,
        "decision": "escalate",
        "tool_name": None,
        "proposed_action": None,
        "escalation_reason": "Tool observations did not produce clear evidence.",
    }


def lexical_terms(query: str) -> List[str]:
    return [token for token in re.split(r"\W+", query.lower()) if len(token) > 2]
