from __future__ import annotations

import json
from typing import Any, Dict, Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.deterministic_policy import (
    ALLOWED_ACTION_TYPES,
    ALLOWED_READ_TOOLS,
    deterministic_decision,
    evidence_from_tools,
    missing_tool,
    summarize_observation,
)
from agent.model_runtime import passive_model_status, record_model_failure, record_model_success
from agent.prompts import build_planner_system_prompt
from agent.state import AgentState
from integrations.actions import normalize_action
from runtime.resilience import run_with_retry
from runtime.settings import get_settings

MIN_ACTION_CONFIDENCE = 0.55


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


class PlannerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str
    target: str | None = None
    namespace: str | None = None
    replicas: int | None = None
    previous_replicas: int | None = None
    reason: str = Field(default="", max_length=400)
    execution_model: str | None = None
    manifest_path: str | None = None
    current_version: str | None = None
    previous_version: str | None = None


class PlannerHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: str = Field(min_length=1, max_length=160)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    evidence_needed: list[str] = Field(default_factory=list)


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thought_summary: str = Field(min_length=1, max_length=400)
    situation_summary: str = Field(default="", max_length=400)
    hypotheses: list[PlannerHypothesis] = Field(default_factory=list, max_length=4)
    hypothesis: str = Field(default="Unknown", min_length=1, max_length=80)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    decision: Literal["call_tool", "propose_action", "escalate"]
    tool_name: str | None = None
    proposed_action: PlannerAction | None = None
    escalation_reason: str | None = Field(default=None, max_length=400, description="One concise sentence explaining escalation; do not repeat the prompt")

    @model_validator(mode="after")
    def validate_branch_payload(self) -> "AgentDecision":
        if self.decision == "call_tool":
            if self.tool_name not in ALLOWED_READ_TOOLS:
                raise ValueError("tool_name must be one of the allowed read tools")
            if self.proposed_action is not None:
                raise ValueError("proposed_action must be empty for call_tool")
        elif self.decision == "propose_action":
            if self.proposed_action is None:
                raise ValueError("proposed_action is required for propose_action")
            if self.proposed_action.action_type not in ALLOWED_ACTION_TYPES:
                raise ValueError("action_type must be one of the allowed structured actions")
        elif self.decision == "escalate" and not (self.escalation_reason or "").strip():
            # Small models sometimes put the explanation only in thought_summary.
            # Reuse their exact text; never change the decision or invent evidence.
            self.escalation_reason = self.thought_summary
        return self


def _planner_context(state: AgentState) -> Dict[str, Any]:
    retrieved = []
    for chunk in state.get("top_chunks", [])[:4]:
        retrieved.append(
            {
                "incident_type": chunk.get("incident_type"),
                "source_file": chunk.get("source_file"),
                "section": chunk.get("section"),
                "text": _compact_text(chunk.get("text", ""), limit=280),
            }
        )

    tool_observations = []
    for tool_name, payload in state.get("tool_results", {}).items():
        tool_observations.append(
            {
                "tool_name": tool_name,
                "summary": summarize_observation(tool_name, payload),
            }
        )

    incident = state["incident"]
    return {
        "incident": {
            "incident_id": incident.get("incident_id"),
            "service": incident.get("service"),
            "namespace": incident.get("namespace"),
            "description": incident.get("description"),
        },
        "seed_hypothesis": state.get("predicted_type", "Unknown"),
        "retrieval_query": state.get("retrieval_query", ""),
        "retrieval_quality": state.get("retrieval_quality", {}),
        "candidate_diagnoses": [
            {
                "incident_type": item.get("incident_type"),
                "score": round(float(item.get("score", item.get("retrieval_confidence", 0.0)) or 0.0), 3),
                "supporting_points": [
                    _compact_text(point, limit=140) for point in list(item.get("supporting_points") or [])[:2]
                ],
                "contradicting_signals": [
                    _compact_text(point, limit=140) for point in list(item.get("contradicting_signals") or [])[:2]
                ],
            }
            for item in state.get("candidate_diagnoses", [])[:3]
        ],
        "specialist_findings": [
            {
                "specialist": item.get("specialist"),
                "status": item.get("status"),
                "confidence": item.get("confidence"),
                "summary": _compact_text(item.get("summary"), limit=160),
                "recommended_next_tools": item.get("recommended_next_tools", []),
            }
            for item in state.get("specialist_findings", [])[:4]
        ],
        "coordinator_summary": {
            "focus": dict(state.get("coordinator_summary") or {}).get("focus", ""),
            "summary": _compact_text(dict(state.get("coordinator_summary") or {}).get("summary", ""), limit=180),
            "recommended_next_tool": dict(state.get("coordinator_summary") or {}).get("recommended_next_tool", ""),
            "reliability": dict(state.get("coordinator_summary") or {}).get("reliability", ""),
        },
        "step_count": int(state.get("step_count", 0)),
        "max_steps": int(state.get("max_steps", 4)),
        "retrieved_runbooks": retrieved,
        "tool_observations": tool_observations,
        "evidence_ledger": [
            {
                "evidence_id": item.get("evidence_id"),
                "tool": item.get("tool"),
                "source": item.get("source"),
                "observed_at": item.get("observed_at"),
                "status": item.get("status"),
                "payload": item.get("payload"),
            }
            for item in state.get("evidence_ledger", [])[-8:]
        ],
        "evidence": evidence_from_tools(state),
        "allowed_tools": ALLOWED_READ_TOOLS,
        "allowed_action_types": ALLOWED_ACTION_TYPES,
    }


def _parse_planner_response(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    if raw_payload.get("done_reason") == "length":
        raise ValueError("Planner exhausted its output token budget before completing the response")
    if isinstance(raw_payload.get("message"), dict):
        content = raw_payload["message"].get("content", "")
    else:
        content = raw_payload.get("response", "")

    if not isinstance(content, str) or not content.strip():
        raise ValueError("Planner returned an empty response body")
    return json.loads(content)


def _call_ollama(state: AgentState) -> AgentDecision:
    settings = get_settings()
    context = _planner_context(state)
    payload = {
        "model": settings.planner_model,
        "stream": False,
        "think": False,
        "format": AgentDecision.model_json_schema(),
        "messages": [
            {
                "role": "system",
                "content": build_planner_system_prompt(ALLOWED_READ_TOOLS, ALLOWED_ACTION_TYPES),
            },
            {
                "role": "user",
                "content": (
                    "Current incident state follows as JSON. "
                    "Return exactly one JSON object that matches the schema.\n"
                    f"{json.dumps(context, indent=2)}"
                ),
            },
        ],
        "options": {
            "temperature": settings.planner_temperature,
            "num_ctx": settings.planner_context_tokens,
            "num_predict": settings.planner_output_tokens,
        },
    }

    req = Request(
        url=f"{settings.planner_base_url.rstrip('/')}/api/chat",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    with urlopen(req, timeout=settings.planner_timeout_seconds) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return AgentDecision.model_validate(_parse_planner_response(raw))


def _call_openai_compatible(state: AgentState) -> AgentDecision:
    settings = get_settings()
    context = _planner_context(state)
    headers = {"Content-Type": "application/json"}
    if settings.planner_api_key:
        headers["Authorization"] = f"Bearer {settings.planner_api_key}"
    payload = {
        "model": settings.planner_model,
        "temperature": settings.planner_temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "agent_decision",
                "schema": AgentDecision.model_json_schema(),
            },
        },
        "messages": [
            {
                "role": "system",
                "content": build_planner_system_prompt(ALLOWED_READ_TOOLS, ALLOWED_ACTION_TYPES),
            },
            {
                "role": "user",
                "content": (
                    "Current incident state follows as JSON. "
                    "Return exactly one JSON object that matches the schema.\n"
                    f"{json.dumps(context, indent=2)}"
                ),
            },
        ],
    }
    req = Request(
        url=f"{settings.planner_base_url.rstrip('/')}/v1/chat/completions",
        headers=headers,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    with urlopen(req, timeout=settings.planner_timeout_seconds) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    choices = list(raw.get("choices", []))
    if not choices:
        raise ValueError("Planner returned no choices")
    if choices[0].get("finish_reason") == "length":
        raise ValueError("Planner exhausted its output token budget before completing the response")
    message = dict((choices[0] or {}).get("message") or {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Planner returned empty content")
    return AgentDecision.model_validate(json.loads(content))


def _deterministic_fallback(state: AgentState) -> AgentDecision:
    return AgentDecision.model_validate(deterministic_decision(state))


def _force_additional_tool(state: AgentState, decision: AgentDecision) -> AgentDecision:
    predicted_type = str(state.get("predicted_type") or "Unknown")
    step_count = int(state.get("step_count", 0))
    max_steps = int(state.get("max_steps", 4))
    next_tool = missing_tool(state)

    if decision.decision != "escalate":
        return decision
    if predicted_type == "Unknown" or step_count > max_steps:
        return decision
    if not next_tool:
        return decision

    return AgentDecision(
        thought_summary=f"Signals still point to {predicted_type}. Gather {next_tool} before escalating.",
        situation_summary=decision.situation_summary,
        hypotheses=decision.hypotheses,
        hypothesis=predicted_type,
        confidence=max(float(decision.confidence or 0.0), 0.25),
        decision="call_tool",
        tool_name=next_tool,
        proposed_action=None,
        escalation_reason=None,
    )


def _apply_deterministic_guardrails(state: AgentState, decision: AgentDecision) -> AgentDecision:
    fallback = AgentDecision.model_validate(deterministic_decision(state))

    if decision.decision == "propose_action" and (
        fallback.decision != "propose_action" or decision.hypothesis != fallback.hypothesis
    ):
        return fallback

    # If the rules already have enough evidence, do not let the model escalate or
    # keep calling extra tools.
    if fallback.decision == "propose_action":
        if decision.decision != "propose_action":
            return fallback
        if float(decision.confidence or 0.0) < MIN_ACTION_CONFIDENCE:
            return fallback
        fallback_action = normalize_action_from_decision(state, fallback)
        decision_action = normalize_action_from_decision(state, decision)
        material_keys = {"action_type", "target", "namespace", "replicas", "previous_replicas", "manifest_path", "previous_version"}
        if any(fallback_action.get(key) != decision_action.get(key) for key in material_keys):
            return fallback

    # A confirmed diagnosis without a catalog action is intentionally
    # escalation-only. The model cannot invent a write merely because the
    # action type exists elsewhere in the global catalog.
    if fallback.decision == "escalate" and str(fallback.hypothesis or "Unknown") != "Unknown":
        return fallback

    # If the model escalates too early or proposes a weak-confidence action,
    # keep gathering the next discriminating tool instead of ending the run.
    if fallback.decision == "call_tool":
        if decision.decision == "escalate":
            return fallback
        if decision.decision == "propose_action":
            return fallback

    return decision


def plan_next_step(state: AgentState) -> tuple[AgentDecision, str, str]:
    settings = get_settings()
    provider = settings.planner_provider

    if provider == "deterministic":
        return _deterministic_fallback(state), "deterministic", ""

    prior_error = str(state.get("planner_error") or "").strip()
    if prior_error:
        return _deterministic_fallback(state), f"{provider}_run_fallback", prior_error

    model_status = passive_model_status()
    if model_status.get("cooldown_active"):
        fallback = _deterministic_fallback(state)
        return fallback, f"{provider}_cooldown_fallback", str(model_status.get("last_error", "Model cooldown is active"))

    try:
        caller = _call_ollama
        operation_name = "planner:ollama"
        backend_name = "ollama"
        if provider in {"openai_compatible", "openai"}:
            caller = _call_openai_compatible
            operation_name = "planner:openai_compatible"
            backend_name = "openai_compatible"
        decision = run_with_retry(
            lambda: caller(state),
            retries=settings.planner_max_retries,
            timeout_seconds=settings.planner_timeout_seconds,
            operation_name=operation_name,
            retry_exceptions=(URLError, TimeoutError, ValidationError, ValueError),
        )
        record_model_success()
        guarded = _force_additional_tool(state, decision)
        guarded = _apply_deterministic_guardrails(state, guarded)
        backend = backend_name
        if guarded.model_dump() != decision.model_dump():
            backend = f"{backend_name}_guardrail"
        return guarded, backend, ""
    except Exception as exc:
        record_model_failure(str(exc))
        fallback = _deterministic_fallback(state)
        backend = "deterministic_fallback" if provider == "auto" else f"{provider}_fallback"
        return fallback, backend, str(exc)


def normalize_action_from_decision(state: AgentState, decision: AgentDecision) -> Dict[str, Any]:
    if decision.proposed_action is None:
        raise ValueError("Planner decision does not contain a proposed action")
    return normalize_action(decision.proposed_action.model_dump(exclude_none=True), state["incident"])
