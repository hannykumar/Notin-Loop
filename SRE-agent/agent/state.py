from __future__ import annotations

from typing import Any, Dict, List
from typing_extensions import TypedDict


class StructuredAction(TypedDict, total=False):
    action_type: str
    target: str
    namespace: str
    replicas: int
    previous_replicas: int
    reason: str
    execution_model: str
    manifest_path: str
    current_version: str
    previous_version: str


class TraceStep(TypedDict, total=False):
    step_id: int
    hypothesis: str
    thought: str
    action: str
    tool_name: str
    tool_args: Dict[str, Any]
    observation: str
    confidence_before: float
    confidence_after: float
    decision: str


class AgentState(TypedDict, total=False):
    run_id: str
    incident_id: str
    incident: Dict[str, Any]
    incident_context: Dict[str, Any]
    service_memory: Dict[str, Any]
    infrastructure_memory: Dict[str, Any]
    integration_summary: List[Dict[str, Any]]
    messages: List[Dict[str, str]]
    trace: List[TraceStep]

    tool_mode: str
    execution_mode: str
    evaluation_profile: str
    approved: bool

    retrieved: List[Dict[str, Any]]
    top_chunks: List[Dict[str, Any]]
    retrieval_query: str
    retrieval_quality: Dict[str, Any]
    retrieval_explanation: Dict[str, Any]
    retrieval_attempts: int
    candidate_diagnoses: List[Dict[str, Any]]
    predicted_type: str
    thought_summary: str
    escalation_reason: str
    planner_backend: str
    planner_error: str

    tool_results: Dict[str, Any]
    next_tool: str
    next_tool_args: Dict[str, Any]
    step_count: int
    max_steps: int

    diagnosis: str
    confidence: float
    confirmed: bool
    requires_human_approval: bool
    approval_prompt: str
    proposed_action: StructuredAction

    plan: List[StructuredAction]
    policy_ok: bool
    policy_violations: List[str]
    sanitized_plan: List[StructuredAction]
    planned_commands: List[str]
    rollback_commands: List[str]

    execution_results: List[Dict[str, Any]]
    rollback_record: Dict[str, Any]
    executor_transport: str
    evidence: Dict[str, Any]
    evidence_ledger: List[Dict[str, Any]]
    hypotheses: List[Dict[str, Any]]
    situation_summary: str
    evidence_gate: Dict[str, Any]
    incident_report: Dict[str, Any]
    claim_validation: Dict[str, Any]
    remediation_options: List[Dict[str, Any]]
    evidence_graph: List[Dict[str, Any]]
    specialist_findings: List[Dict[str, Any]]
    evidence_after: Dict[str, Any]
    verification: Dict[str, Any]
    improved: bool
    improvement_summary: str
    latency_metrics: Dict[str, Any]
    coordinator_summary: Dict[str, Any]
    investigation_activity: List[Dict[str, Any]]
    incident_brief: Dict[str, Any]
    approval_summary: Dict[str, Any]
    handoff_summary: Dict[str, Any]
    verification_summary: Dict[str, Any]
    tool_confirmation_required: bool
    tool_confirmation_prompt: str
    tool_confirmation_tool: str
    model_status: Dict[str, Any]
    runtime_health: Dict[str, Any]

    final_response: str
    rca_draft: str
    rca_final: str

    saved_trace: str
