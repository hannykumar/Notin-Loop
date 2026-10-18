"""
agent/langgraph_agent.py

Clean LangGraph workflow for the MCP-native SRE agent.

Graph:
Ingest -> Agent -> Tools -> Agent -> ...
                    |-> HITL_Interrupt -> END
                    |-> Execute -> END
                    |-> END
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from langgraph.graph import END, StateGraph

from agent.deterministic_policy import (
    ensure_tool_prerequisites,
    evidence_from_tools,
    summarize_observation,
    tool_args_for,
)
from agent.evidence_graph import build_evidence_graph, rank_diagnoses
from agent.evidence_ledger import (
    alert_evidence,
    evaluate_evidence_gate,
    hypothesis_snapshot,
    tool_evidence,
    validated_model_hypotheses,
)
from agent.incident_catalog import catalog_version
from agent.incident_context import incident_payload_from_context
from agent.grounding import build_grounded_report
from agent.model_runtime import passive_model_status
from agent.planner import normalize_action_from_decision, plan_next_step
from agent.prompts import SYSTEM_PROMPT, UNKNOWN_ESCALATION_OUTPUT
from agent.retrieval import build_retry_query, retrieve_runbook_chunks, summarize_retrieval_context
from agent.run_summaries import (
    build_approval_summary,
    build_handoff_summary,
    build_incident_brief,
    build_verification_summary,
)
from agent.service_memory import enabled_integrations_summary, load_service_memory
from agent.specialists import build_specialist_findings, coordinate_specialist_findings
from agent.state import AgentState
from executor.client import ExecutorClient
from integrations.actions import render_action_to_command, rollback_command_for_action
from integrations.mock_mcp import MockMCP
from integrations.tool_gateway import get_tool_client
from runtime.artifacts import save_run_artifacts
from runtime.metrics import PLANNER_DURATION, RETRIEVAL_DURATION, TOOL_DURATION, observe_duration
from runtime.settings import get_settings
from agent.tracing import create_trace


os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
MAX_STEPS = 4
MIN_CONFIDENCE_FOR_ACTION = 0.55


def append_message(state: AgentState, role: str, content: str) -> None:
    state.setdefault("messages", []).append({"role": role, "content": content})


def trace_add(state: AgentState, event_type: str, **payload: Any) -> None:
    state.setdefault("trace", []).append({"type": event_type, **payload})


def normalize_execution_mode(value: Any) -> str:
    mode = str(value or "simulate").strip().lower()
    aliases = {
        "dry_run": "preview",
        "preview": "preview",
        "mock": "simulate",
        "simulate": "simulate",
        "real": "live",
        "live": "live",
    }
    return aliases.get(mode, "simulate")


def normalize_tool_mode(value: Any) -> str:
    mode = str(value or "direct").strip().lower()
    aliases = {
        "local": "direct",
        "direct": "direct",
        "mcp_mock": "mcp",
        "mcp": "mcp",
    }
    return aliases.get(mode, "direct")


def load_incident(incident_id: str) -> Dict[str, Any]:
    return MockMCP(incident_id).data


def _retrieve_chunks(
    query: str,
    *,
    limit: int,
    incident: Dict[str, Any] | None,
    evidence: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    """
    Compatibility wrapper: older tests monkeypatch this module-level function with a
    simpler `(query, limit)` signature, while the production retrieval path accepts
    richer context.
    """
    try:
        return retrieve_runbook_chunks(query, limit=limit, incident=incident, evidence=evidence)
    except TypeError:
        return retrieve_runbook_chunks(query, limit=limit)


def _evidence_domain_count(evidence: Dict[str, Any]) -> int:
    domains = 0
    if dict(evidence.get("metrics") or {}):
        domains += 1
    if list(evidence.get("logs_tail") or []):
        domains += 1
    if list(evidence.get("cluster_events") or []):
        domains += 1
    if dict(evidence.get("prometheus") or {}):
        domains += 1
    if dict(evidence.get("loki") or {}):
        domains += 1
    if dict(evidence.get("tempo") or {}):
        domains += 1
    if list(evidence.get("recent_deploys") or []):
        domains += 1
    if dict(evidence.get("service_dependencies") or {}):
        domains += 1
    return domains


def _refresh_analysis(
    state: AgentState,
    *,
    evidence: Dict[str, Any],
    candidate_seed: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    seed_candidates = list(candidate_seed if candidate_seed is not None else (state.get("candidate_diagnoses") or []))
    specialist_findings = build_specialist_findings(evidence, (state.get("service_memory") or {}).get("payload", {}))
    seeded_state = {
        **state,
        "evidence": evidence,
        "specialist_findings": specialist_findings,
        "candidate_diagnoses": seed_candidates,
    }
    diagnosis_candidates = rank_diagnoses(seeded_state)
    coordinator_summary = coordinate_specialist_findings(specialist_findings, diagnosis_candidates)
    evidence_graph = build_evidence_graph(
        {
            **seeded_state,
            "candidate_diagnoses": diagnosis_candidates,
            "coordinator_summary": coordinator_summary,
        }
    )
    return {
        "specialist_findings": specialist_findings,
        "coordinator_summary": coordinator_summary,
        "evidence_graph": evidence_graph,
        "candidate_diagnoses": diagnosis_candidates,
    }


def _finalize_operator_views(state: AgentState) -> Dict[str, Any]:
    brief = build_incident_brief(state)
    approval = build_approval_summary(state)
    handoff = build_handoff_summary(state)
    verification = build_verification_summary(state)
    return {
        "incident_brief": brief,
        "approval_summary": approval,
        "handoff_summary": handoff,
        "verification_summary": verification,
    }


def build_final_response(state: AgentState) -> str:
    diagnosis = state.get("diagnosis", "Unknown")
    if diagnosis == "Unknown" or not state.get("confirmed", False):
        return UNKNOWN_ESCALATION_OUTPUT

    brief = dict(state.get("incident_brief") or build_incident_brief(state))
    approval_summary = dict(state.get("approval_summary") or build_approval_summary(state))
    planned_commands = list(state.get("planned_commands") or [])
    top_candidate = (state.get("candidate_diagnoses") or [{}])[0]
    citations = list(top_candidate.get("citations") or [])[:4]
    if not citations:
        citations = [str(node.get("evidence_id", "")) for node in list(state.get("evidence_graph") or [])[:4] if node.get("evidence_id")]

    lines = [
        f"Diagnosis: {diagnosis}.",
        brief.get("summary", "The investigation gathered enough evidence to support this diagnosis."),
    ]
    lines.append(
        "Coordinator summary: "
        + str((state.get("coordinator_summary") or {}).get("summary") or "The investigation gathered enough evidence to support this diagnosis.")
    )
    if brief.get("top_evidence"):
        lines.append("Top evidence: " + " | ".join(list(brief.get("top_evidence") or [])[:3]))
    if brief.get("uncertainties"):
        lines.append("Still unclear: " + " | ".join(list(brief.get("uncertainties") or [])[:2]))
    if planned_commands:
        lines.append(f"Proposed action: {planned_commands[0]}")
        if approval_summary.get("why_this_action"):
            lines.append("Why this action: " + str(approval_summary.get("why_this_action")))
        lines.append("Execution still requires explicit human approval.")
    else:
        lines.append("No executable catalog action met the observed safety preconditions.")
        lines.append("Recommended next step: hand off the evidence and diagnosis to the on-call SRE for a scoped repair decision.")
    if brief.get("alternatives"):
        lines.append("Other possibilities still visible: " + ", ".join(list(brief.get("alternatives") or [])[:2]))
    if citations:
        lines.append("Citations: " + ", ".join(citations))
    return "\n".join(lines)


def _build_investigation_activity(
    *,
    retrieval_summary: str,
    coordinator_summary: Dict[str, Any],
    tool_name: str = "",
    tool_observation: str = "",
    next_step: str = "",
) -> List[Dict[str, Any]]:
    activity = [
        {"stage": "intake", "status": "completed", "summary": "Loaded incident context and service memory."},
        {"stage": "retrieval", "status": "completed", "summary": retrieval_summary},
    ]
    activity.extend(list(coordinator_summary.get("activity") or []))
    if tool_name:
        activity.append({"stage": "tool", "status": "completed", "summary": f"Collected {tool_name}: {tool_observation}"})
    if next_step:
        activity.append({"stage": "next_step", "status": "ready", "summary": next_step})
    return activity


def _tool_confirmation_metadata(tool_name: str, integrations: List[Dict[str, Any]]) -> Dict[str, Any]:
    for integration in integrations:
        tools = list(integration.get("tools") or [])
        if tool_name not in tools:
            continue
        confirmation_mode = str(integration.get("confirmation_mode", "auto"))
        safety_level = str(integration.get("safety_level", "low"))
        if confirmation_mode == "manual":
            return {
                "tool_confirmation_required": True,
                "tool_confirmation_tool": tool_name,
                "tool_confirmation_prompt": f"Confirm tool call for {tool_name} against {integration.get('name', 'external integration')} ({safety_level}).",
            }
    return {
        "tool_confirmation_required": False,
        "tool_confirmation_tool": "",
        "tool_confirmation_prompt": "",
    }


def node_ingest(state: AgentState) -> Dict[str, Any]:
    incident = state.get("incident") or load_incident(state["incident_id"])
    service_memory = load_service_memory(str(incident.get("service", "")), namespace=str(incident.get("namespace", "prod")))
    integration_summary = enabled_integrations_summary()
    retrieval_limit = max(get_settings().retrieval_limit, 3)
    retrieval_started = time.perf_counter()
    retrieval_enabled = state.get("evaluation_profile") != "llm_no_retrieval"
    context = dict(incident.get("incident_context") or {})
    labels = dict(context.get("labels") or {})
    retrieval_query = " ".join(
        item
        for item in [
            str(incident.get("title") or "").strip(),
            str(incident.get("description") or "").strip(),
            str(labels.get("scenario") or "").replace("_", " ").strip(),
        ]
        if item
    )
    with observe_duration(RETRIEVAL_DURATION):
        retrieved = (
            _retrieve_chunks(retrieval_query, limit=retrieval_limit, incident=incident, evidence={})
            if retrieval_enabled
            else []
        )
        retrieval = summarize_retrieval_context(retrieval_query, retrieved, incident=incident)
    retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000.0, 2)
    retrieved = retrieval["retrieved"]
    predicted_type = retrieval["predicted_type"]

    append_message(state, "system", SYSTEM_PROMPT)
    append_message(state, "user", incident["description"])

    trace_add(state, "ingest", incident_id=incident["incident_id"], predicted_type=predicted_type)
    trace_add(
        state,
        "retrieval",
        query=retrieval["query"],
        top_chunks=retrieved[:5],
        retrieval_quality=retrieval["retrieval_quality"],
        retrieval_explanation=retrieval.get("retrieval_explanation", {}),
        candidate_diagnoses=retrieval["candidate_diagnoses"][:3],
    )

    initial_state = {
        "incident": incident,
        "service_memory": service_memory,
        "integration_summary": integration_summary,
        "retrieved": retrieved,
        "top_chunks": retrieved[:5],
        "retrieval_query": retrieval["query"],
        "retrieval_quality": retrieval["retrieval_quality"],
        "retrieval_explanation": retrieval.get("retrieval_explanation", {}),
        "retrieval_attempts": 0,
        "candidate_diagnoses": retrieval["candidate_diagnoses"],
        "predicted_type": predicted_type,
    }
    analysis = _refresh_analysis(initial_state, evidence={}, candidate_seed=retrieval["candidate_diagnoses"])
    initial_findings = analysis["specialist_findings"]
    coordinator_summary = analysis["coordinator_summary"]
    evidence_graph = analysis["evidence_graph"]
    diagnosis_candidates = analysis["candidate_diagnoses"]
    investigation_activity = _build_investigation_activity(
        retrieval_summary=f"Retrieved {len(retrieved[:5])} grounded runbook chunks for the initial hypothesis.",
        coordinator_summary=coordinator_summary,
        next_step=f"Coordinator focus: {coordinator_summary.get('focus', 'metrics')}",
    )
    seeded_state = {
        **initial_state,
        "diagnosis": diagnosis_candidates[0]["incident_type"] if diagnosis_candidates else predicted_type,
        "confidence": float((diagnosis_candidates[0] if diagnosis_candidates else {}).get("score", 0.0) or 0.0),
        "confirmed": False,
        "evidence_graph": evidence_graph,
        "specialist_findings": initial_findings,
        "coordinator_summary": coordinator_summary,
        "candidate_diagnoses": diagnosis_candidates,
        "investigation_activity": investigation_activity,
    }
    initial_ledger = [alert_evidence(state["incident_context"])] if state.get("incident_context") else []
    if initial_ledger:
        evidence_graph = build_evidence_graph({**seeded_state, "evidence_ledger": initial_ledger})
        seeded_state["evidence_graph"] = evidence_graph
    operator_views = _finalize_operator_views(seeded_state)
    return {
        "incident": incident,
        "service_memory": service_memory,
        "infrastructure_memory": service_memory,
        "retrieved": retrieved,
        "top_chunks": retrieved[:5],
        "retrieval_query": retrieval["query"],
        "retrieval_quality": retrieval["retrieval_quality"],
        "retrieval_attempts": 0,
        "candidate_diagnoses": diagnosis_candidates,
        "predicted_type": predicted_type,
        "thought_summary": "",
        "escalation_reason": "",
        "planner_backend": "",
        "planner_error": "",
        "tool_results": {},
        "tool_mode": normalize_tool_mode(state.get("tool_mode", "mcp")),
        "messages": state.get("messages", []),
        "trace": state.get("trace", []),
        "step_count": 0,
        "max_steps": MAX_STEPS,
        "diagnosis": "Unknown",
        "confidence": 0.0,
        "confirmed": False,
        "requires_human_approval": False,
        "policy_ok": False,
        "policy_violations": [],
        "sanitized_plan": [],
        "plan": [],
        "execution_results": [],
        "planned_commands": [],
        "rollback_commands": [],
        "evidence": {},
        "evidence_ledger": initial_ledger,
        "hypotheses": hypothesis_snapshot(diagnosis_candidates, evidence_graph),
        "evidence_gate": evaluate_evidence_gate(diagnosis_candidates, evidence_graph),
        "evidence_graph": evidence_graph,
        "specialist_findings": initial_findings,
        "coordinator_summary": coordinator_summary,
        "investigation_activity": investigation_activity,
        "incident_brief": operator_views["incident_brief"],
        "approval_summary": operator_views["approval_summary"],
        "handoff_summary": operator_views["handoff_summary"],
        "verification_summary": operator_views["verification_summary"],
        "tool_confirmation_required": False,
        "tool_confirmation_prompt": "",
        "tool_confirmation_tool": "",
        "model_status": passive_model_status(),
        "runtime_health": {},
        "evidence_after": {},
        "improved": False,
        "improvement_summary": "",
        "latency_metrics": {"retrieval_ms": retrieval_ms},
    }


def node_agent(state: AgentState) -> Dict[str, Any]:
    step_count = int(state.get("step_count", 0)) + 1

    evidence = evidence_from_tools(state)
    planning_state = {**state, "step_count": step_count, "evidence": evidence}
    analysis = _refresh_analysis(planning_state, evidence=evidence)
    specialist_findings = analysis["specialist_findings"]
    coordinator_summary = analysis["coordinator_summary"]
    diagnosis_candidates = analysis["candidate_diagnoses"]
    evidence_graph = analysis["evidence_graph"]
    hypotheses = hypothesis_snapshot(diagnosis_candidates, evidence_graph)
    evidence_gate = evaluate_evidence_gate(diagnosis_candidates, evidence_graph)
    planning_state.update(
        {
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
            "candidate_diagnoses": diagnosis_candidates,
            "evidence_graph": evidence_graph,
            "hypotheses": hypotheses,
            "evidence_gate": evidence_gate,
        }
    )
    retrieval_attempts = int(state.get("retrieval_attempts", 0))
    retry_limit = max(get_settings().retrieval_retry_limit, 0)
    retrieval_limit = max(get_settings().retrieval_limit, 3)
    if (
        state.get("evaluation_profile") != "llm_no_retrieval"
        and
        step_count > 1
        and retrieval_attempts < retry_limit
        and (
            state.get("predicted_type", "Unknown") == "Unknown"
            or bool((state.get("retrieval_quality") or {}).get("needs_retry"))
            or float(state.get("confidence", 0.0)) < 0.35
        )
    ):
        retry_query = build_retry_query(state["incident"], evidence, state.get("candidate_diagnoses", []))
        retry_started = time.perf_counter()
        with observe_duration(RETRIEVAL_DURATION):
            retry_chunks = _retrieve_chunks(retry_query, limit=retrieval_limit, incident=state["incident"], evidence=evidence)
            retry_retrieval = summarize_retrieval_context(retry_query, retry_chunks, incident=state["incident"])
        trace_add(
            state,
            "retrieval_retry",
            query=retry_query,
            top_chunks=retry_retrieval["top_chunks"],
            retrieval_quality=retry_retrieval["retrieval_quality"],
            retrieval_explanation=retry_retrieval.get("retrieval_explanation", {}),
            candidate_diagnoses=retry_retrieval["candidate_diagnoses"][:3],
        )
        updated_latency = dict(state.get("latency_metrics", {}))
        updated_latency["retrieval_retry_ms"] = round((time.perf_counter() - retry_started) * 1000.0, 2)
        planning_state.update(
            {
                "retrieved": retry_retrieval["retrieved"],
                "top_chunks": retry_retrieval["top_chunks"],
                "retrieval_query": retry_retrieval["query"],
                "retrieval_quality": retry_retrieval["retrieval_quality"],
                "retrieval_explanation": retry_retrieval.get("retrieval_explanation", {}),
                "retrieval_attempts": retrieval_attempts + 1,
                "candidate_diagnoses": retry_retrieval["candidate_diagnoses"],
                "predicted_type": retry_retrieval["predicted_type"],
                "latency_metrics": updated_latency,
            }
        )
        analysis = _refresh_analysis(planning_state, evidence=evidence, candidate_seed=retry_retrieval["candidate_diagnoses"])
        specialist_findings = analysis["specialist_findings"]
        coordinator_summary = analysis["coordinator_summary"]
        diagnosis_candidates = analysis["candidate_diagnoses"]
        evidence_graph = analysis["evidence_graph"]
        hypotheses = hypothesis_snapshot(diagnosis_candidates, evidence_graph)
        evidence_gate = evaluate_evidence_gate(diagnosis_candidates, evidence_graph)
        planning_state.update(
            {
                "specialist_findings": specialist_findings,
                "coordinator_summary": coordinator_summary,
                "candidate_diagnoses": diagnosis_candidates,
                "evidence_graph": evidence_graph,
                "hypotheses": hypotheses,
                "evidence_gate": evidence_gate,
            }
        )

    planner_started = time.perf_counter()
    with observe_duration(PLANNER_DURATION):
        decision, planner_backend, planner_error = plan_next_step(planning_state)
    model_hypotheses = validated_model_hypotheses(decision.hypotheses, evidence_graph)
    if model_hypotheses:
        hypotheses = model_hypotheses
    situation_summary = decision.situation_summary.strip() or str(
        (coordinator_summary or {}).get("summary") or state["incident"].get("description", "")
    )
    thought = decision.thought_summary.strip()
    confidence = float(decision.confidence)
    diagnosis_name = decision.hypothesis.strip() or "Unknown"
    next_predicted_type = diagnosis_name if diagnosis_name != "Unknown" else planning_state.get("predicted_type", "Unknown")
    updated_latency = dict(planning_state.get("latency_metrics", {}))
    updated_latency["planner_ms"] = round((time.perf_counter() - planner_started) * 1000.0, 2)
    evaluation_profile = str(state.get("evaluation_profile") or "hybrid_full")
    tools_allowed = evaluation_profile not in {"llm_no_retrieval", "llm_rag"}
    full_evidence_controls = evaluation_profile in {"", "hybrid_full", "deterministic_baseline"}

    append_message(state, "assistant", thought)

    retrieval_quality = dict(planning_state.get("retrieval_quality") or {})
    strong_evidence = _evidence_domain_count(evidence) >= 2
    top_candidate = dict(diagnosis_candidates[0] if diagnosis_candidates else {})
    close_second = False
    if len(diagnosis_candidates) > 1:
        close_second = abs(
            float(diagnosis_candidates[0].get("score", 0.0) or 0.0)
            - float(diagnosis_candidates[1].get("score", 0.0) or 0.0)
        ) < 0.12
    strong_rank = float(top_candidate.get("score", 0.0) or 0.0) >= 0.6 and not close_second
    operator_views = _finalize_operator_views(
        {
            **planning_state,
            "diagnosis": diagnosis_name,
            "confidence": confidence,
            "evidence_graph": evidence_graph,
            "hypotheses": hypotheses,
            "evidence_gate": evidence_gate,
            "situation_summary": situation_summary,
            "candidate_diagnoses": diagnosis_candidates,
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
        }
    )
    claim_validation = build_grounded_report(
        {
            **planning_state,
            "diagnosis": diagnosis_name,
            "confirmed": diagnosis_name != "Unknown",
            "evidence_graph": evidence_graph,
            "candidate_diagnoses": diagnosis_candidates,
            "hypotheses": hypotheses,
            "situation_summary": situation_summary,
        }
    )["claim_validation"]
    real_alert_controls = bool(full_evidence_controls and state.get("incident_context"))
    evidence_strength_ok = (
        bool(evidence_gate.get("passed")) and bool(claim_validation.get("critical_claims_supported"))
        if real_alert_controls
        else strong_evidence or float(retrieval_quality.get("overall", 0.0)) >= 0.72
    )
    diagnosis_supported = (
        diagnosis_name != "Unknown"
        and diagnosis_name == top_candidate.get("incident_type")
        and confidence >= MIN_CONFIDENCE_FOR_ACTION
        and strong_rank
        and evidence_strength_ok
    )
    if decision.decision == "propose_action" and diagnosis_supported:
        proposed_action = normalize_action_from_decision(planning_state, decision)
        planned_command = render_action_to_command(proposed_action, state["incident"])
        rollback = rollback_command_for_action(proposed_action, state["incident"])
        action_views = _finalize_operator_views(
            {
                **planning_state,
                "diagnosis": diagnosis_name,
                "confidence": confidence,
                "confirmed": True,
                "evidence_graph": evidence_graph,
                "candidate_diagnoses": diagnosis_candidates,
                "specialist_findings": specialist_findings,
                "coordinator_summary": coordinator_summary,
                "proposed_action": proposed_action,
                "requires_human_approval": True,
                "planned_commands": [planned_command],
                "rollback_commands": [rollback],
            }
        )

        trace_add(
            state,
            "agent_decision",
            step_id=step_count,
            hypothesis=diagnosis_name,
            thought=thought,
            confidence_before=state.get("confidence", 0.0),
            confidence_after=confidence,
            decision="propose_remediation",
            planner_backend=planner_backend,
            planner_error=planner_error,
        )
        investigation_activity = _build_investigation_activity(
            retrieval_summary=f"Retrieved {len(planning_state.get('top_chunks', [])[:5])} grounded runbook chunks.",
            coordinator_summary=coordinator_summary,
            next_step="Prepared a remediation recommendation and paused for human approval.",
        )

        return {
            "step_count": step_count,
            "predicted_type": next_predicted_type,
            "diagnosis": diagnosis_name,
            "confidence": confidence,
            "confirmed": True,
            "evidence": evidence,
            "evidence_graph": evidence_graph,
            "hypotheses": hypotheses,
            "evidence_gate": evidence_gate,
            "claim_validation": claim_validation,
            "situation_summary": situation_summary,
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
            "investigation_activity": investigation_activity,
            "incident_brief": action_views["incident_brief"],
            "approval_summary": action_views["approval_summary"],
            "handoff_summary": action_views["handoff_summary"],
            "verification_summary": action_views["verification_summary"],
            "proposed_action": proposed_action,
            "requires_human_approval": True,
            "plan": [proposed_action],
            "sanitized_plan": [proposed_action],
            "policy_ok": True,
            "planned_commands": [planned_command],
            "rollback_commands": [rollback],
            "thought_summary": thought,
            "escalation_reason": "",
            "planner_backend": planner_backend,
            "planner_error": planner_error,
            "retrieved": planning_state.get("retrieved", []),
            "top_chunks": planning_state.get("top_chunks", []),
            "retrieval_query": planning_state.get("retrieval_query", ""),
            "retrieval_quality": planning_state.get("retrieval_quality", {}),
            "retrieval_explanation": planning_state.get("retrieval_explanation", {}),
            "retrieval_attempts": planning_state.get("retrieval_attempts", retrieval_attempts),
            "candidate_diagnoses": diagnosis_candidates,
            "latency_metrics": updated_latency,
            "tool_confirmation_required": False,
            "tool_confirmation_prompt": "",
            "tool_confirmation_tool": "",
            "model_status": passive_model_status(),
        }

    if decision.decision == "propose_action" and tools_allowed and step_count <= int(state.get("max_steps", MAX_STEPS)):
        next_tool = str(coordinator_summary.get("recommended_next_tool") or "").strip() or "get_cluster_events"
        next_tool = ensure_tool_prerequisites(next_tool, planning_state)
        tool_args = tool_args_for(next_tool, planning_state)
        tool_confirmation = _tool_confirmation_metadata(next_tool, state.get("integration_summary", []))
        investigation_activity = _build_investigation_activity(
            retrieval_summary=f"Retrieved {len(planning_state.get('top_chunks', [])[:5])} grounded runbook chunks.",
            coordinator_summary=coordinator_summary,
            next_step=f"The model tried to propose remediation early, so the workflow is collecting more evidence with {next_tool}.",
        )
        trace_add(
            state,
            "agent_decision",
            step_id=step_count,
            hypothesis=next_predicted_type,
            thought=thought,
            action="call_tool",
            tool_name=next_tool,
            tool_args=tool_args,
            confidence_before=state.get("confidence", 0.0),
            confidence_after=confidence,
            decision="guardrail_more_evidence",
            planner_backend=planner_backend,
            planner_error=planner_error,
        )
        return {
            "step_count": step_count,
            "predicted_type": next_predicted_type,
            "next_tool": next_tool,
            "next_tool_args": tool_args,
            "diagnosis": "Unknown",
            "confidence": confidence,
            "confirmed": False,
            "evidence": evidence,
            "hypotheses": hypotheses,
            "evidence_gate": evidence_gate,
            "claim_validation": claim_validation,
            "situation_summary": situation_summary,
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
            "investigation_activity": investigation_activity,
            "incident_brief": operator_views["incident_brief"],
            "approval_summary": operator_views["approval_summary"],
            "handoff_summary": operator_views["handoff_summary"],
            "verification_summary": operator_views["verification_summary"],
            "thought_summary": thought,
            "planner_backend": planner_backend,
            "planner_error": planner_error,
            "retrieved": planning_state.get("retrieved", []),
            "top_chunks": planning_state.get("top_chunks", []),
            "retrieval_query": planning_state.get("retrieval_query", ""),
            "retrieval_quality": retrieval_quality,
            "retrieval_explanation": planning_state.get("retrieval_explanation", {}),
            "retrieval_attempts": planning_state.get("retrieval_attempts", retrieval_attempts),
            "candidate_diagnoses": diagnosis_candidates,
            "evidence_graph": evidence_graph,
            "latency_metrics": updated_latency,
            "model_status": passive_model_status(),
            **tool_confirmation,
        }

    if (
        decision.decision == "call_tool"
        and tools_allowed
        and decision.tool_name
        and step_count <= int(state.get("max_steps", MAX_STEPS))
    ):
        next_tool = ensure_tool_prerequisites(decision.tool_name, planning_state)
        tool_args = tool_args_for(next_tool, planning_state)
        tool_confirmation = _tool_confirmation_metadata(next_tool, state.get("integration_summary", []))
        investigation_activity = _build_investigation_activity(
            retrieval_summary=f"Retrieved {len(planning_state.get('top_chunks', [])[:5])} grounded runbook chunks.",
            coordinator_summary=coordinator_summary,
            next_step=f"Next the assistant will collect {next_tool}.",
        )

        trace_add(
            state,
            "agent_decision",
            step_id=step_count,
            hypothesis=next_predicted_type,
            thought=thought,
            action="call_tool",
            tool_name=next_tool,
            tool_args=tool_args,
            confidence_before=state.get("confidence", 0.0),
            confidence_after=confidence,
            decision="tool_call",
            planner_backend=planner_backend,
            planner_error=planner_error,
        )

        return {
            "step_count": step_count,
            "predicted_type": next_predicted_type,
            "next_tool": next_tool,
            "next_tool_args": tool_args,
            "diagnosis": "Unknown",
            "confidence": confidence,
            "confirmed": False,
            "evidence": evidence,
            "hypotheses": hypotheses,
            "evidence_gate": evidence_gate,
            "claim_validation": claim_validation,
            "situation_summary": situation_summary,
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
            "investigation_activity": investigation_activity,
            "incident_brief": operator_views["incident_brief"],
            "approval_summary": operator_views["approval_summary"],
            "handoff_summary": operator_views["handoff_summary"],
            "verification_summary": operator_views["verification_summary"],
            "thought_summary": thought,
            "planner_backend": planner_backend,
            "planner_error": planner_error,
            "retrieved": planning_state.get("retrieved", []),
            "top_chunks": planning_state.get("top_chunks", []),
            "retrieval_query": planning_state.get("retrieval_query", ""),
            "retrieval_quality": planning_state.get("retrieval_quality", {}),
            "retrieval_explanation": planning_state.get("retrieval_explanation", {}),
            "retrieval_attempts": planning_state.get("retrieval_attempts", retrieval_attempts),
            "candidate_diagnoses": diagnosis_candidates,
            "evidence_graph": evidence_graph,
            "latency_metrics": updated_latency,
            "model_status": passive_model_status(),
            **tool_confirmation,
        }

    escalation_reason = decision.escalation_reason or "Tool observations did not produce clear evidence."
    if decision.decision == "escalate" and diagnosis_supported:
        confirmed_views = _finalize_operator_views(
            {
                **planning_state,
                "diagnosis": diagnosis_name,
                "confidence": confidence,
                "confirmed": True,
                "evidence_graph": evidence_graph,
                "candidate_diagnoses": diagnosis_candidates,
                "specialist_findings": specialist_findings,
                "coordinator_summary": coordinator_summary,
                "requires_human_approval": False,
                "planned_commands": [],
                "rollback_commands": [],
            }
        )
        trace_add(
            state,
            "agent_decision",
            step_id=step_count,
            hypothesis=diagnosis_name,
            thought=thought,
            confidence_before=state.get("confidence", 0.0),
            confidence_after=confidence,
            decision="diagnosed_escalation",
            escalation_reason=escalation_reason,
            planner_backend=planner_backend,
            planner_error=planner_error,
        )
        investigation_activity = _build_investigation_activity(
            retrieval_summary=f"Retrieved {len(planning_state.get('top_chunks', [])[:5])} grounded runbook chunks.",
            coordinator_summary=coordinator_summary,
            next_step="The diagnosis is supported, but no preconditioned catalog action is safe; hand off to the on-call SRE.",
        )
        return {
            "step_count": step_count,
            "predicted_type": next_predicted_type,
            "diagnosis": diagnosis_name,
            "confidence": confidence,
            "confirmed": True,
            "evidence": evidence,
            "evidence_graph": evidence_graph,
            "hypotheses": hypotheses,
            "evidence_gate": evidence_gate,
            "claim_validation": claim_validation,
            "situation_summary": situation_summary,
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
            "investigation_activity": investigation_activity,
            "incident_brief": confirmed_views["incident_brief"],
            "approval_summary": confirmed_views["approval_summary"],
            "handoff_summary": confirmed_views["handoff_summary"],
            "verification_summary": confirmed_views["verification_summary"],
            "planned_commands": [],
            "rollback_commands": [],
            "requires_human_approval": False,
            "policy_ok": True,
            "plan": [],
            "sanitized_plan": [],
            "thought_summary": thought,
            "escalation_reason": escalation_reason,
            "planner_backend": planner_backend,
            "planner_error": planner_error,
            "retrieved": planning_state.get("retrieved", []),
            "top_chunks": planning_state.get("top_chunks", []),
            "retrieval_query": planning_state.get("retrieval_query", ""),
            "retrieval_quality": planning_state.get("retrieval_quality", {}),
            "retrieval_explanation": planning_state.get("retrieval_explanation", {}),
            "retrieval_attempts": planning_state.get("retrieval_attempts", retrieval_attempts),
            "candidate_diagnoses": diagnosis_candidates,
            "latency_metrics": updated_latency,
            "tool_confirmation_required": False,
            "tool_confirmation_prompt": "",
            "tool_confirmation_tool": "",
            "model_status": passive_model_status(),
        }

    append_message(state, "assistant", UNKNOWN_ESCALATION_OUTPUT)
    trace_add(
        state,
        "agent_decision",
        step_id=step_count,
        hypothesis="Unknown",
        thought=thought,
        confidence_before=state.get("confidence", 0.0),
        confidence_after=min(confidence or 0.2, 0.2),
        decision="escalate",
        escalation_reason=escalation_reason,
        planner_backend=planner_backend,
        planner_error=planner_error,
    )
    investigation_activity = _build_investigation_activity(
        retrieval_summary=f"Retrieved {len(planning_state.get('top_chunks', [])[:5])} grounded runbook chunks.",
        coordinator_summary=coordinator_summary,
        next_step="Evidence stayed weak, so the run escalated instead of guessing.",
    )

    return {
        "step_count": step_count,
        "diagnosis": "Unknown",
        "confidence": min(confidence or 0.2, 0.2),
        "confirmed": False,
        "evidence": evidence,
        "evidence_graph": evidence_graph,
        "hypotheses": hypotheses,
        "evidence_gate": evidence_gate,
        "claim_validation": claim_validation,
        "situation_summary": situation_summary,
        "specialist_findings": specialist_findings,
        "coordinator_summary": coordinator_summary,
        "investigation_activity": investigation_activity,
        "incident_brief": operator_views["incident_brief"],
        "approval_summary": operator_views["approval_summary"],
        "handoff_summary": operator_views["handoff_summary"],
        "verification_summary": operator_views["verification_summary"],
        "final_response": UNKNOWN_ESCALATION_OUTPUT,
        "planned_commands": [],
        "rollback_commands": [],
        "requires_human_approval": False,
        "policy_ok": False,
        "plan": [],
        "sanitized_plan": [],
        "thought_summary": thought,
        "escalation_reason": escalation_reason,
        "planner_backend": planner_backend,
        "planner_error": planner_error,
        "retrieved": planning_state.get("retrieved", []),
        "top_chunks": planning_state.get("top_chunks", []),
        "retrieval_query": planning_state.get("retrieval_query", ""),
        "retrieval_quality": planning_state.get("retrieval_quality", {}),
        "retrieval_explanation": planning_state.get("retrieval_explanation", {}),
        "retrieval_attempts": planning_state.get("retrieval_attempts", retrieval_attempts),
        "candidate_diagnoses": diagnosis_candidates,
        "latency_metrics": updated_latency,
        "tool_confirmation_required": False,
        "tool_confirmation_prompt": "",
        "tool_confirmation_tool": "",
        "model_status": passive_model_status(),
    }


def node_tools(state: AgentState) -> Dict[str, Any]:
    tool_name = state["next_tool"]
    tool_args = state.get("next_tool_args", {})
    client = get_tool_client(state.get("tool_mode", "mcp"), state["incident_id"], allow_write=False)
    tool_started = time.perf_counter()
    with observe_duration(TOOL_DURATION, tool_name=tool_name):
        observation = client.call_tool(tool_name, **tool_args)
    tool_duration_ms = (time.perf_counter() - tool_started) * 1000.0

    updated_results = dict(state.get("tool_results", {}))
    updated_results[tool_name] = observation
    observation_summary = summarize_observation(tool_name, observation)
    updated_ledger = list(state.get("evidence_ledger") or [])
    updated_ledger.append(
        tool_evidence(
            ledger=updated_ledger,
            tool_name=tool_name,
            tool_args=tool_args,
            observation=observation,
            incident_context=state.get("incident_context"),
            duration_ms=tool_duration_ms,
        )
    )

    append_message(state, "tool", f"{tool_name}: {observation_summary}")
    trace_add(
        state,
        "tool_observation",
        tool_name=tool_name,
        tool_args=tool_args,
        observation=observation_summary,
    )

    updated_evidence = evidence_from_tools({**state, "tool_results": updated_results})
    analysis = _refresh_analysis(
        {**state, "tool_results": updated_results, "evidence_ledger": updated_ledger},
        evidence=updated_evidence,
    )
    specialist_findings = analysis["specialist_findings"]
    coordinator_summary = analysis["coordinator_summary"]
    evidence_graph = analysis["evidence_graph"]
    diagnosis_candidates = analysis["candidate_diagnoses"]
    operator_views = _finalize_operator_views(
        {
            **state,
            "tool_results": updated_results,
            "evidence": updated_evidence,
            "evidence_graph": evidence_graph,
            "specialist_findings": specialist_findings,
            "coordinator_summary": coordinator_summary,
            "candidate_diagnoses": diagnosis_candidates,
        }
    )
    return {
        "tool_results": updated_results,
        "evidence": updated_evidence,
        "evidence_ledger": updated_ledger,
        "hypotheses": hypothesis_snapshot(diagnosis_candidates, evidence_graph),
        "evidence_gate": evaluate_evidence_gate(diagnosis_candidates, evidence_graph),
        "evidence_graph": evidence_graph,
        "specialist_findings": specialist_findings,
        "coordinator_summary": coordinator_summary,
        "candidate_diagnoses": diagnosis_candidates,
        "incident_brief": operator_views["incident_brief"],
        "approval_summary": operator_views["approval_summary"],
        "handoff_summary": operator_views["handoff_summary"],
        "verification_summary": operator_views["verification_summary"],
        "investigation_activity": _build_investigation_activity(
            retrieval_summary=f"Retrieved {len(state.get('top_chunks', [])[:5])} grounded runbook chunks.",
            coordinator_summary=coordinator_summary,
            tool_name=tool_name,
            tool_observation=observation_summary,
            next_step="The planner will review the new observation and decide whether to continue or recommend an action.",
        ),
        "next_tool": "",
        "next_tool_args": {},
        "tool_confirmation_required": False,
        "tool_confirmation_prompt": "",
        "tool_confirmation_tool": "",
        "model_status": passive_model_status(),
        "latency_metrics": {
            **dict(state.get("latency_metrics", {})),
            f"tool_{tool_name}_ms": round((time.perf_counter() - tool_started) * 1000.0, 2),
        },
    }


def node_hitl_interrupt(state: AgentState) -> Dict[str, Any]:
    approval = dict(state.get("approval_summary") or build_approval_summary(state))
    command = state.get("planned_commands", ["(none)"])[0]
    prompt = (
        f"{approval.get('summary', 'Review the proposed action.')}"
        f" Command: {command}. Risk: {approval.get('risk_level', 'unknown')}."
    )
    trace_add(state, "hitl_interrupt", prompt=prompt, action=state.get("proposed_action", {}))
    return {"approval_prompt": prompt, "approval_summary": approval}


def node_execute(state: AgentState) -> Dict[str, Any]:
    action = state.get("proposed_action", {})
    incident = state["incident"]
    mode = normalize_execution_mode(state.get("execution_mode", "preview"))
    planned_command = render_action_to_command(action, incident)

    if mode == "preview":
        result = {"status": "preview", "tool": "execute_remediation", "command": planned_command, "action": action}
        trace_add(state, "execute", mode=mode, result=result)
        output = {
            "execution_results": [result],
            "rollback_record": {
                "status": "ready" if state.get("rollback_commands") else "not_available",
                "rollback_command": state.get("rollback_commands", [""])[0] if state.get("rollback_commands") else "",
            },
            "executor_transport": "none",
            "evidence_after": {},
            "verification": {},
            "improved": False,
            "improvement_summary": "Preview only. No changes were applied.",
        }
        output["verification_summary"] = build_verification_summary({**state, **output})
        return output

    execution_id = f"{state.get('run_id') or 'local'}_{uuid4().hex[:8]}"
    response = ExecutorClient().execute(
        {
            "execution_id": execution_id,
            "run_id": state.get("run_id", ""),
            "incident_id": state["incident_id"],
            "incident": incident,
            "action": action,
            "diagnosis": state.get("diagnosis", "Unknown"),
            "execution_mode": mode,
            "tool_mode": state.get("tool_mode", "mcp"),
            "evidence_before": state.get("evidence", {}),
            "rollback_commands": state.get("rollback_commands", []),
        }
    )
    trace_add(
        state,
        "execute",
        mode=mode,
        result=response.get("execution_results", [{}])[0] if response.get("execution_results") else {},
        improvement={
            "improved": response.get("improved", False),
            "summary": response.get("improvement_summary", ""),
        },
        executor_transport=response.get("executor_transport", "embedded"),
    )

    output = {
        "execution_results": response.get("execution_results", []),
        "rollback_record": response.get("rollback_record", {}),
        "executor_transport": response.get("executor_transport", "embedded"),
        "evidence_after": response.get("evidence_after", {}),
        "verification": response.get("verification", {}),
        "improved": bool(response.get("improved", False)),
        "improvement_summary": response.get("improvement_summary", ""),
    }
    output["verification_summary"] = build_verification_summary({**state, **output})
    return output


def route_after_agent(state: AgentState) -> str:
    if state.get("proposed_action"):
        return "execute" if bool(state.get("approved", False)) else "interrupt"
    if state.get("next_tool"):
        return "tools"
    if state.get("final_response"):
        return "end"
    return "end"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("ingest", node_ingest)
    graph.add_node("agent", node_agent)
    graph.add_node("tools", node_tools)
    graph.add_node("hitl_interrupt", node_hitl_interrupt)
    graph.add_node("execute", node_execute)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "interrupt": "hitl_interrupt",
            "execute": "execute",
            "end": END,
        },
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("hitl_interrupt", END)
    graph.add_edge("execute", END)
    return graph.compile()


GRAPH = build_graph()


def persist_trace(state: AgentState) -> str:
    trace = create_trace(state["incident_id"])
    for event in state.get("trace", []):
        event_type = event.get("type", "trace")
        trace.add(event_type, **{k: v for k, v in event.items() if k != "type"})
    return str(trace.save())


def run_incident_langgraph(
    incident_id: str,
    approved: bool = False,
    execution_mode: str = "preview",
    tool_mode: str = "mcp",
    run_id: str = "",
    incident_payload: Dict[str, Any] | None = None,
    incident_context: Dict[str, Any] | None = None,
    evaluation_profile: str = "hybrid_full",
    save_artifacts_enabled: bool = True,
) -> Dict[str, Any]:
    resolved_payload = incident_payload
    if incident_context:
        fixture = incident_payload
        if fixture is None:
            try:
                fixture = load_incident(incident_id)
            except FileNotFoundError:
                fixture = None
        resolved_payload = incident_payload_from_context(incident_context, fixture=fixture)
        MockMCP.seed_live_state(incident_id, resolved_payload)
    elif incident_payload is not None:
        MockMCP.seed_live_state(incident_id, incident_payload)

    final_state = GRAPH.invoke(
        {
            "run_id": run_id,
            "incident_id": incident_id,
            "incident": resolved_payload or {},
            "incident_context": incident_context or {},
            "approved": approved,
            "execution_mode": normalize_execution_mode(execution_mode),
            "tool_mode": normalize_tool_mode(tool_mode),
            "evaluation_profile": evaluation_profile,
        }
    )

    final_state["final_response"] = build_final_response(final_state)
    final_state["saved_trace"] = persist_trace(final_state)
    final_state["rca_draft"] = final_state["final_response"]
    final_state["rca_final"] = final_state["final_response"]
    final_state.update(_finalize_operator_views(final_state))
    final_state.update(build_grounded_report(final_state))

    result = {
        "run_id": run_id,
        "incident_id": incident_id,
        "incident": final_state.get("incident", {}),
        "incident_context": final_state.get("incident_context", incident_context or {}),
        "system_prompt": SYSTEM_PROMPT,
        "execution_mode": normalize_execution_mode(execution_mode),
        "tool_mode": normalize_tool_mode(tool_mode),
        "evaluation_profile": evaluation_profile,
        "predicted_type": final_state.get("predicted_type", "Unknown"),
        "planner_backend": final_state.get("planner_backend", ""),
        "planner_error": final_state.get("planner_error", ""),
        "diagnosis": final_state.get("diagnosis", "Unknown"),
        "confidence": float(final_state.get("confidence", 0.0)),
        "confirmed": bool(final_state.get("confirmed", False)),
        "top_chunks": final_state.get("top_chunks", []),
        "retrieved": final_state.get("retrieved", []),
        "retrieval_query": final_state.get("retrieval_query", ""),
        "retrieval_quality": final_state.get("retrieval_quality", {}),
        "retrieval_explanation": final_state.get("retrieval_explanation", {}),
        "candidate_diagnoses": final_state.get("candidate_diagnoses", []),
        "service_memory": final_state.get("service_memory", {}),
        "infrastructure_memory": final_state.get("infrastructure_memory", final_state.get("service_memory", {})),
        "integration_summary": final_state.get("integration_summary", []),
        "messages": final_state.get("messages", []),
        "trace": final_state.get("trace", []),
        "evidence": final_state.get("evidence", {}),
        "evidence_ledger": final_state.get("evidence_ledger", []),
        "hypotheses": final_state.get("hypotheses") or hypothesis_snapshot(final_state.get("candidate_diagnoses", []), final_state.get("evidence_graph", [])),
        "situation_summary": final_state.get("situation_summary", ""),
        "evidence_gate": evaluate_evidence_gate(final_state.get("candidate_diagnoses", []), final_state.get("evidence_graph", [])),
        "incident_report": final_state.get("incident_report", {}),
        "claim_validation": final_state.get("claim_validation", {}),
        "remediation_options": final_state.get("remediation_options", []),
        "evidence_graph": final_state.get("evidence_graph", []),
        "specialist_findings": final_state.get("specialist_findings", []),
        "coordinator_summary": final_state.get("coordinator_summary", {}),
        "investigation_activity": final_state.get("investigation_activity", []),
        "tool_confirmation_required": bool(final_state.get("tool_confirmation_required", False)),
        "tool_confirmation_prompt": final_state.get("tool_confirmation_prompt", ""),
        "tool_confirmation_tool": final_state.get("tool_confirmation_tool", ""),
        "model_status": final_state.get("model_status", passive_model_status()),
        "runtime_health": final_state.get("runtime_health", {}),
        "thought_summary": final_state.get("thought_summary", ""),
        "escalation_reason": final_state.get("escalation_reason", ""),
        "incident_brief": final_state.get("incident_brief", {}),
        "approval_summary": final_state.get("approval_summary", {}),
        "handoff_summary": final_state.get("handoff_summary", {}),
        "verification_summary": final_state.get("verification_summary", {}),
        "plan": final_state.get("plan", []),
        "proposed_action": final_state.get("proposed_action", {}),
        "requires_human_approval": bool(final_state.get("requires_human_approval", False)),
        "approval_prompt": final_state.get("approval_prompt", ""),
        "policy_ok": bool(final_state.get("policy_ok", False)),
        "policy_violations": final_state.get("policy_violations", []),
        "sanitized_plan": final_state.get("sanitized_plan", []),
        "planned_commands": final_state.get("planned_commands", []),
        "rollback_commands": final_state.get("rollback_commands", []),
        "rollback_record": final_state.get("rollback_record", {}),
        "executor_transport": final_state.get("executor_transport", ""),
        "execution_results": final_state.get("execution_results", []),
        "evidence_after": final_state.get("evidence_after", {}),
        "verification": final_state.get("verification", {}),
        "improved": bool(final_state.get("improved", False)),
        "improvement_summary": final_state.get("improvement_summary", ""),
        "final_response": final_state.get("final_response", ""),
        "rca_draft": final_state.get("rca_draft", ""),
        "rca_final": final_state.get("rca_final", ""),
        "saved_trace": final_state.get("saved_trace", ""),
        "latency_metrics": final_state.get("latency_metrics", {}),
        "catalog_version": catalog_version(),
    }
    decisions = [event for event in result.get("trace", []) if event.get("type") == "agent_decision"]
    errors = list(dict.fromkeys(str(event["planner_error"]) for event in decisions if event.get("planner_error")))
    if errors:
        result["planner_error"] = "; ".join(errors)
    result["planner_fallback_count"] = sum(bool(event.get("planner_error")) or "fallback" in str(event.get("planner_backend", "")) for event in decisions)
    verification = dict(result.get("verification") or {})
    if verification:
        result["verification_outcome"] = str(
            verification.get("status") or ("resolved" if verification.get("resolved") else "unchanged")
        ).strip().lower()
    else:
        result["verification_outcome"] = "not_run"

    if save_artifacts_enabled:
        artifact_dir = save_run_artifacts(result)
        result["artifact_dir"] = str(artifact_dir)

    return result
