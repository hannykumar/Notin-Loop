from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from runtime.db import session_scope
from runtime.models import (
    ApprovalRecord,
    AuditEvent,
    ExecutionRecord,
    IncidentLearningRecord,
    IntegrationRegistryRecord,
    OperatorFeedbackRecord,
    RollbackRecord,
    RunRecord,
    ServiceMemoryRecord,
)
from integrations.actions import rollback_action_for_action
from agent.approval import build_proposal_artifact, verify_proposal_artifact


SERVICE_CONTEXT_PATH = Path("config/service_context.json")


def _bootstrap_service_context() -> Dict[str, Any]:
    if not SERVICE_CONTEXT_PATH.exists():
        return {}
    return json.loads(SERVICE_CONTEXT_PATH.read_text(encoding="utf-8"))


def _default_integrations() -> List[Dict[str, Any]]:
    return [
        {
            "integration_id": "mcp-mock",
            "name": "Mock MCP",
            "integration_type": "mcp",
            "enabled": "true",
            "base_url": "",
            "auth_mode": "none",
            "enabled_tools": {"mode": "mock", "tools": ["get_pod_status", "get_metrics", "get_pod_logs", "get_recent_deploys"]},
            "scope": {"environment": "local"},
            "health_status": "ready",
            "details": {
                "description": "Built-in mock MCP tool surface for local development.",
                "safety_level": "low",
                "confirmation_mode": "auto",
                "product_role": "telemetry_connector",
            },
        },
        {
            "integration_id": "prometheus-live",
            "name": "Prometheus",
            "integration_type": "metrics",
            "enabled": "true",
            "base_url": "",
            "auth_mode": "none",
            "enabled_tools": {"tools": ["get_metrics", "query_prometheus"]},
            "scope": {"environment": "live"},
            "health_status": "unknown",
            "details": {
                "description": "Live Prometheus adapter for metrics and verification.",
                "safety_level": "low",
                "confirmation_mode": "auto",
                "product_role": "telemetry_connector",
            },
        },
        {
            "integration_id": "loki-live",
            "name": "Loki",
            "integration_type": "logs",
            "enabled": "true",
            "base_url": "",
            "auth_mode": "none",
            "enabled_tools": {"tools": ["query_loki", "get_pod_logs"]},
            "scope": {"environment": "live"},
            "health_status": "unknown",
            "details": {
                "description": "Live Loki adapter for log retrieval.",
                "safety_level": "medium",
                "confirmation_mode": "suggest",
                "product_role": "telemetry_connector",
            },
        },
        {
            "integration_id": "tempo-live",
            "name": "Tempo",
            "integration_type": "traces",
            "enabled": "true",
            "base_url": "",
            "auth_mode": "none",
            "enabled_tools": {"tools": ["query_tempo"]},
            "scope": {"environment": "live"},
            "health_status": "unknown",
            "details": {
                "description": "Live Tempo adapter for trace context.",
                "safety_level": "medium",
                "confirmation_mode": "suggest",
                "product_role": "telemetry_connector",
            },
        },
        {
            "integration_id": "grafana-alert-adapter",
            "name": "Grafana Alert Adapter",
            "integration_type": "alert_source",
            "enabled": "true",
            "base_url": "",
            "auth_mode": "shared_token",
            "enabled_tools": {"ingest_paths": ["/alerts/grafana/webhook"]},
            "scope": {"environment": "demo", "platform": "grafana"},
            "health_status": "ready",
            "details": {
                "description": "Alert-source adapter for Grafana-managed webhooks.",
                "safety_level": "low",
                "confirmation_mode": "auto",
                "product_role": "alert_source",
            },
        },
        {
            "integration_id": "generic-alert-adapter",
            "name": "Generic Alert Adapter",
            "integration_type": "alert_source",
            "enabled": "true",
            "base_url": "",
            "auth_mode": "shared_token",
            "enabled_tools": {"ingest_paths": ["/alerts/events"]},
            "scope": {"environment": "external", "platform": "generic"},
            "health_status": "ready",
            "details": {
                "description": "Platform-agnostic alert webhook adapter for non-Grafana monitoring systems.",
                "safety_level": "low",
                "confirmation_mode": "auto",
                "product_role": "alert_source",
            },
        },
    ]


def _now_iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_payload(run: RunRecord, approval: ApprovalRecord | None = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "run_id": run.run_id,
        "created_at": _now_iso(run.created_at),
        "status": run.status,
        "request": run.request_payload or {},
        "plan_result": run.plan_result,
        "execute_result": run.execute_result,
    }
    if approval is not None:
        stored_proposal = dict(approval.proposed_action or {})
        is_artifact = str(stored_proposal.get("artifact_version") or "") == "1"
        payload["approval"] = {
            "status": approval.status,
            "approver": approval.approver,
            "prompt": approval.prompt,
            "proposed_action": stored_proposal.get("action", {}) if is_artifact else stored_proposal,
            "artifact": stored_proposal if is_artifact else {},
            "approved_at": _now_iso(approval.updated_at) if approval.status == "approved" else "",
            "updated_at": _now_iso(approval.updated_at),
        }
    return payload


def _service_memory_payload(record: ServiceMemoryRecord) -> Dict[str, Any]:
    return {
        "service": record.service,
        "namespace": record.namespace,
        "summary": record.summary,
        "payload": record.payload or {},
        "source": record.source,
        "created_at": _now_iso(record.created_at),
        "updated_at": _now_iso(record.updated_at),
    }


def _integration_payload(record: IntegrationRegistryRecord) -> Dict[str, Any]:
    details = record.details or {}
    return {
        "integration_id": record.integration_id,
        "name": record.name,
        "integration_type": record.integration_type,
        "enabled": str(record.enabled).lower() == "true",
        "base_url": record.base_url,
        "auth_mode": record.auth_mode,
        "enabled_tools": record.enabled_tools or {},
        "scope": record.scope or {},
        "health_status": record.health_status,
        "safety_level": str(details.get("safety_level", "unknown")),
        "confirmation_mode": str(details.get("confirmation_mode", "auto")),
        "product_role": str(details.get("product_role", "")),
        "metadata": details,
        "created_at": _now_iso(record.created_at),
        "updated_at": _now_iso(record.updated_at),
    }


def _payload_service_name(payload: Dict[str, Any]) -> str:
    request = dict(payload.get("request") or {})
    for candidate in [
        (payload.get("plan_result") or {}).get("service_memory", {}).get("service"),
        (payload.get("execute_result") or {}).get("service_memory", {}).get("service"),
        request.get("service"),
        request.get("linked_service"),
    ]:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _payload_incident_type(payload: Dict[str, Any]) -> str:
    active = dict(payload.get("execute_result") or payload.get("plan_result") or {})
    return str(active.get("diagnosis") or "").strip()


def create_run(run_id: str, request_payload: Dict[str, Any]) -> Dict[str, Any]:
    with session_scope() as session:
        run = RunRecord(
            run_id=run_id,
            incident_id=str(request_payload.get("incident_id", "")),
            status="planning",
            execution_mode=str(request_payload.get("execution_mode", "preview")),
            tool_mode=str(request_payload.get("tool_mode", "mcp")),
            request_payload=request_payload,
        )
        session.add(run)
        session.flush()
        return _run_payload(run)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as session:
        run = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        if run is None:
            return None
        approval = session.scalar(select(ApprovalRecord).where(ApprovalRecord.run_id == run_id).order_by(ApprovalRecord.id.desc()))
        return _run_payload(run, approval)


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    with session_scope() as session:
        runs = session.scalars(select(RunRecord).order_by(RunRecord.created_at.desc()).limit(limit)).all()
        return [_run_payload(run) for run in runs]


def list_runs_for_service(service: str, limit: int = 10) -> List[Dict[str, Any]]:
    target = str(service or "").strip().lower()
    if not target:
        return []
    rows = list_runs(limit=max(limit * 6, 50))
    matched = [row for row in rows if _payload_service_name(row).strip().lower() == target]
    return matched[:limit]


def list_runs_for_diagnosis(incident_type: str, limit: int = 10) -> List[Dict[str, Any]]:
    target = str(incident_type or "").strip().lower()
    if not target:
        return []
    rows = list_runs(limit=max(limit * 6, 50))
    matched = [row for row in rows if _payload_incident_type(row).strip().lower() == target]
    return matched[:limit]


def list_successful_actions(service: str = "", incident_type: str = "", limit: int = 10) -> List[Dict[str, Any]]:
    rows = list_runs(limit=max(limit * 8, 80))
    service_target = str(service or "").strip().lower()
    incident_target = str(incident_type or "").strip().lower()
    results: List[Dict[str, Any]] = []
    for row in rows:
        if service_target and _payload_service_name(row).strip().lower() != service_target:
            continue
        if incident_target and _payload_incident_type(row).strip().lower() != incident_target:
            continue
        execute = dict(row.get("execute_result") or {})
        verification = dict(execute.get("verification") or {})
        status = str(verification.get("status") or ("resolved" if verification.get("resolved") else "")).strip().lower()
        if status not in {"resolved", "improved"}:
            continue
        action = dict(execute.get("proposed_action") or row.get("plan_result", {}).get("proposed_action") or {})
        if not action:
            continue
        results.append(
            {
                "run_id": row.get("run_id"),
                "service": _payload_service_name(row),
                "incident_type": _payload_incident_type(row),
                "action_type": action.get("action_type", ""),
                "target": action.get("target", ""),
                "reason": action.get("reason", ""),
                "verification_status": status,
            }
        )
    return results[:limit]


def update_plan_result(run_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    with session_scope() as session:
        run = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        if run is None:
            raise KeyError(run_id)
        run.plan_result = result
        run.status = "awaiting_approval" if result.get("requires_human_approval") else "planned"
        run.trace_path = str(result.get("saved_trace", ""))
        run.artifact_dir = str(result.get("artifact_dir", ""))
        run.approval_status = "pending" if result.get("requires_human_approval") else "not_required"
        session.flush()
        approval = None
        if result.get("requires_human_approval"):
            proposal = build_proposal_artifact(run_id, result)
            approval = ApprovalRecord(
                run_id=run_id,
                status="pending",
                prompt=str(result.get("approval_prompt", "")),
                proposed_action=proposal,
            )
            session.add(approval)
            session.flush()
        return _run_payload(run, approval)


def mark_approval(run_id: str, approver: str, status: str = "approved", *, execution_mode: str | None = None) -> Dict[str, Any]:
    with session_scope() as session:
        claimed = session.execute(
            update(RunRecord).where(
                RunRecord.run_id == run_id,
                RunRecord.status == "awaiting_approval",
                RunRecord.approval_status == "pending",
            ).values(status=status, approval_status=status)
        )
        if claimed.rowcount != 1:
            raise ValueError("This run has no pending approval")
        run = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        approval = session.scalar(select(ApprovalRecord).where(ApprovalRecord.run_id == run_id).order_by(ApprovalRecord.id.desc()))
        if run is None:
            raise KeyError(run_id)
        if approval is None or approval.status != "pending":
            raise ValueError("This run has no pending approval artifact")
        if status == "approved":
            verify_proposal_artifact(dict(approval.proposed_action or {}), dict(run.plan_result or {}))
        approval.status = status
        approval.approver = approver
        run.approval_status = status
        if status == "approved":
            run.status = "approved"
        if execution_mode is not None:
            run.request_payload = {**dict(run.request_payload or {}), "execution_mode": execution_mode}
            run.execution_mode = execution_mode
        session.flush()
        return _run_payload(run, approval)


def update_execution_result(run_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    with session_scope() as session:
        run = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        approval = session.scalar(select(ApprovalRecord).where(ApprovalRecord.run_id == run_id).order_by(ApprovalRecord.id.desc()))
        if run is None:
            raise KeyError(run_id)
        run.execute_result = result
        verification = dict(result.get("verification") or {})
        verification_outcome = str(verification.get("status") or ("resolved" if verification.get("resolved") else "")).strip().lower()
        run.status = verification_outcome if verification_outcome in {
            "resolved",
            "improved",
            "unchanged",
            "regressed",
            "inconclusive",
            "verification_timeout",
        } else "completed"
        run.execution_mode = str(result.get("execution_mode", run.execution_mode or "preview"))
        run.executed_at = datetime.now(timezone.utc)
        run.trace_path = str(result.get("saved_trace", run.trace_path or ""))
        run.artifact_dir = str(result.get("artifact_dir", run.artifact_dir or ""))

        execution_results = result.get("execution_results", [])
        preview_result = execution_results[0] if execution_results else {}
        preview_status = str(preview_result.get("status", ""))
        existing_execution = session.scalar(
            select(ExecutionRecord).where(ExecutionRecord.run_id == run_id).order_by(ExecutionRecord.created_at.desc())
        )
        if preview_status == "preview" and existing_execution is None:
            session.add(
                ExecutionRecord(
                    execution_id=f"{run_id}_preview",
                    run_id=run_id,
                    execution_mode=str(result.get("execution_mode", "preview")),
                    status="preview",
                    command=str(preview_result.get("command", "")),
                    action=preview_result.get("action", {}),
                    result_payload=result,
                )
            )

        rollback_record = result.get("rollback_record", {})
        rollback_command = str(rollback_record.get("rollback_command", "")).strip()
        existing_rollback = session.scalar(
            select(RollbackRecord).where(RollbackRecord.run_id == run_id).order_by(RollbackRecord.created_at.desc())
        )
        if rollback_command and existing_rollback is None:
            rollback_action = None
            proposed_action = result.get("proposed_action", {})
            if proposed_action:
                rollback_action = rollback_action_for_action(proposed_action, result)
            session.add(
                RollbackRecord(
                    run_id=run_id,
                    execution_id=f"{run_id}_preview",
                    status=str(rollback_record.get("status", "ready")),
                    rollback_command=rollback_command,
                    payload={
                        "source": "preview",
                        "rollback_record": rollback_record,
                        "rollback_action": rollback_action,
                        "action": proposed_action,
                    },
                )
            )
        existing_learning = session.scalar(select(IncidentLearningRecord).where(IncidentLearningRecord.run_id == run_id))
        if existing_learning is None:
            context = dict(result.get("incident_context") or {})
            incident = dict(result.get("incident") or {})
            session.add(
                IncidentLearningRecord(
                    run_id=run_id,
                    service=str(context.get("service") or incident.get("service") or "unknown"),
                    namespace=str(context.get("namespace") or incident.get("namespace") or "default"),
                    incident_type=str(result.get("diagnosis") or "Unknown"),
                    diagnosis_confirmed=str(bool(result.get("confirmed"))).lower(),
                    action=dict(result.get("proposed_action") or {}),
                    verification=verification,
                    evidence_pattern={
                        "evidence_ids": [item.get("evidence_id") for item in result.get("evidence_ledger", [])],
                        "evidence_gate": result.get("evidence_gate", {}),
                        "groundedness": (result.get("claim_validation") or {}).get("groundedness"),
                    },
                )
            )
        session.flush()
        return _run_payload(run, approval)


def mark_run_status(run_id: str, status: str) -> Dict[str, Any]:
    with session_scope() as session:
        run = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        approval = session.scalar(select(ApprovalRecord).where(ApprovalRecord.run_id == run_id).order_by(ApprovalRecord.id.desc()))
        if run is None:
            raise KeyError(run_id)
        run.status = status
        session.flush()
        return _run_payload(run, approval)


def log_audit_event(run_id: str, event_type: str, payload: Dict[str, Any], actor: str = "system") -> None:
    with session_scope() as session:
        session.add(AuditEvent(run_id=run_id, event_type=event_type, actor=actor, payload=payload))


def list_audit_events(run_id: str) -> List[Dict[str, Any]]:
    with session_scope() as session:
        rows = session.scalars(select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.created_at.asc())).all()
        return [
            {
                "event_type": row.event_type,
                "actor": row.actor,
                "payload": row.payload,
                "created_at": _now_iso(row.created_at),
            }
            for row in rows
        ]


def get_or_create_service_memory(service: str, namespace: str = "prod") -> Dict[str, Any] | None:
    service_name = str(service or "").strip()
    if not service_name:
        return None
    with session_scope() as session:
        record = session.scalar(
            select(ServiceMemoryRecord)
            .where(ServiceMemoryRecord.service == service_name)
            .where(ServiceMemoryRecord.namespace == namespace)
        )
        if record is not None:
            return _service_memory_payload(record)

        bootstrap = _bootstrap_service_context().get("services", {}).get(service_name)
        if not bootstrap:
            return None

        summary = (
            f"Service {service_name} is owned by {((bootstrap.get('owner') or {}).get('team') or 'unknown')} "
            f"and has {len(bootstrap.get('dashboards', []))} dashboards, "
            f"{len(bootstrap.get('recent_deploys', []))} recent deploys, and "
            f"{len(bootstrap.get('incident_history', []))} historical incidents."
        )
        record = ServiceMemoryRecord(
            service=service_name,
            namespace=namespace,
            summary=summary,
            payload=bootstrap,
            source="bootstrap",
        )
        session.add(record)
        session.flush()
        return _service_memory_payload(record)


def list_service_memory(limit: int = 100) -> List[Dict[str, Any]]:
    bootstrap = _bootstrap_service_context().get("services", {})
    for service_name in bootstrap:
        get_or_create_service_memory(service_name)
    with session_scope() as session:
        rows = session.scalars(
            select(ServiceMemoryRecord)
            .order_by(ServiceMemoryRecord.service.asc())
            .limit(limit)
        ).all()
        return [_service_memory_payload(row) for row in rows]


def get_infrastructure_memory(service: str, namespace: str = "prod") -> Dict[str, Any] | None:
    return get_or_create_service_memory(service, namespace=namespace)


def list_infrastructure_memory(limit: int = 100) -> List[Dict[str, Any]]:
    return list_service_memory(limit=limit)


def list_incident_learning(limit: int = 100) -> List[Dict[str, Any]]:
    with session_scope() as session:
        rows = session.scalars(
            select(IncidentLearningRecord).order_by(IncidentLearningRecord.created_at.desc()).limit(limit)
        ).all()
        return [
            {
                "run_id": row.run_id,
                "service": row.service,
                "namespace": row.namespace,
                "incident_type": row.incident_type,
                "diagnosis_confirmed": row.diagnosis_confirmed == "true",
                "action": row.action or {},
                "verification": row.verification or {},
                "evidence_pattern": row.evidence_pattern or {},
                "created_at": _now_iso(row.created_at),
            }
            for row in rows
        ]


def _operator_feedback_payload(record: OperatorFeedbackRecord) -> Dict[str, Any]:
    return {
        "run_id": record.run_id,
        "actor": record.actor,
        "rating": record.rating,
        "diagnosis_correct": None if record.diagnosis_correct == "unknown" else record.diagnosis_correct == "true",
        "action_helpful": None if record.action_helpful == "unknown" else record.action_helpful == "true",
        "notes": record.notes,
        "incident_snapshot": record.incident_snapshot or {},
        "created_at": _now_iso(record.created_at),
        "updated_at": _now_iso(record.updated_at),
    }


def save_operator_feedback(
    run_id: str,
    *,
    actor: str,
    rating: int,
    diagnosis_correct: bool | None,
    action_helpful: bool | None,
    notes: str,
) -> Dict[str, Any]:
    with session_scope() as session:
        run = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        if run is None:
            raise KeyError(run_id)
        result = dict(run.execute_result or run.plan_result or {})
        context = dict(result.get("incident_context") or (run.request_payload or {}).get("incident_context") or {})
        incident = dict(result.get("incident") or {})
        snapshot = {
            "service": str(context.get("service") or incident.get("service") or "unknown"),
            "namespace": str(context.get("namespace") or incident.get("namespace") or "default"),
            "incident_type": str(result.get("diagnosis") or "Unknown"),
            "verification_outcome": str(
                (result.get("verification") or {}).get("status")
                or (result.get("verification") or {}).get("outcome")
                or result.get("verification_outcome")
                or ""
            ),
            "executed": bool(run.execute_result) and result.get("execution_mode") != "preview",
        }
        record = session.scalar(
            select(OperatorFeedbackRecord)
            .where(OperatorFeedbackRecord.run_id == run_id)
            .where(OperatorFeedbackRecord.actor == actor)
            .order_by(OperatorFeedbackRecord.id.desc())
        )
        values = {
            "rating": rating,
            "diagnosis_correct": "unknown" if diagnosis_correct is None else str(diagnosis_correct).lower(),
            "action_helpful": "unknown" if action_helpful is None else str(action_helpful).lower(),
            "notes": notes,
            "incident_snapshot": snapshot,
        }
        if record is None:
            record = OperatorFeedbackRecord(run_id=run_id, actor=actor, **values)
            session.add(record)
        else:
            for key, value in values.items():
                setattr(record, key, value)
        session.flush()
        return _operator_feedback_payload(record)


def list_operator_feedback(*, service: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    with session_scope() as session:
        rows = session.scalars(
            select(OperatorFeedbackRecord).order_by(OperatorFeedbackRecord.updated_at.desc()).limit(limit)
        ).all()
        payloads = [_operator_feedback_payload(row) for row in rows]
        if service:
            payloads = [item for item in payloads if item["incident_snapshot"].get("service") == service]
        return payloads[:limit]


def _ensure_integrations_seeded(session) -> None:
    existing = {
        row.integration_id: row
        for row in session.scalars(select(IntegrationRegistryRecord)).all()
    }
    for payload in _default_integrations():
        current = existing.get(str(payload.get("integration_id")))
        if current is None:
            session.add(IntegrationRegistryRecord(**payload))
            continue
        current.name = str(payload.get("name", current.name))
        current.integration_type = str(payload.get("integration_type", current.integration_type))
        current.auth_mode = str(payload.get("auth_mode", current.auth_mode))
        current.enabled_tools = payload.get("enabled_tools", current.enabled_tools)
        current.scope = payload.get("scope", current.scope)
        current.health_status = str(payload.get("health_status", current.health_status))
        details = dict(current.details or {})
        details.update(payload.get("details", {}))
        current.details = details
    session.flush()


def list_integrations(limit: int = 100) -> List[Dict[str, Any]]:
    with session_scope() as session:
        _ensure_integrations_seeded(session)
        rows = session.scalars(
            select(IntegrationRegistryRecord)
            .order_by(IntegrationRegistryRecord.integration_type.asc(), IntegrationRegistryRecord.name.asc())
            .limit(limit)
        ).all()
        return [_integration_payload(row) for row in rows]


def get_execution_by_id(execution_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as session:
        record = session.scalar(select(ExecutionRecord).where(ExecutionRecord.execution_id == execution_id))
        return record.result_payload if record else None


def record_execution(
    execution_id: str,
    run_id: str,
    execution_mode: str,
    command: str,
    action: Dict[str, Any],
    result_payload: Dict[str, Any],
) -> None:
    with session_scope() as session:
        session.add(
            ExecutionRecord(
                execution_id=execution_id,
                run_id=run_id,
                execution_mode=execution_mode,
                status=str(result_payload.get("status", "completed")),
                command=command,
                action=action,
                result_payload=result_payload,
            )
        )


def record_rollback(execution_id: str, run_id: str, rollback_command: str, payload: Dict[str, Any]) -> None:
    with session_scope() as session:
        session.add(
            RollbackRecord(
                run_id=run_id,
                execution_id=execution_id,
                rollback_command=rollback_command,
                payload=payload,
            )
        )


def finish_execution(execution_id: str, result_payload: Dict[str, Any]) -> None:
    with session_scope() as session:
        record = session.scalar(select(ExecutionRecord).where(ExecutionRecord.execution_id == execution_id))
        if record is None:
            raise KeyError(execution_id)
        record.status = str(result_payload.get("status", "completed"))
        record.command = str(result_payload.get("command", record.command or ""))
        record.result_payload = result_payload


def get_latest_rollback(run_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as session:
        record = session.scalar(select(RollbackRecord).where(RollbackRecord.run_id == run_id).order_by(RollbackRecord.created_at.desc()))
        if record is None:
            return None
        return {
            "execution_id": record.execution_id,
            "status": record.status,
            "rollback_command": record.rollback_command,
            "payload": record.payload,
            "created_at": _now_iso(record.created_at),
        }


def update_latest_rollback_status(run_id: str, status: str, payload: Dict[str, Any] | None = None) -> Optional[Dict[str, Any]]:
    with session_scope() as session:
        record = session.scalar(select(RollbackRecord).where(RollbackRecord.run_id == run_id).order_by(RollbackRecord.created_at.desc()))
        if record is None:
            return None
        record.status = status
        if payload:
            merged = dict(record.payload or {})
            merged.update(payload)
            record.payload = merged
        session.flush()
        return {
            "execution_id": record.execution_id,
            "status": record.status,
            "rollback_command": record.rollback_command,
            "payload": record.payload,
            "created_at": _now_iso(record.created_at),
        }
