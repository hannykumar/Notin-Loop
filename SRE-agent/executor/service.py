from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.exc import IntegrityError
from agent.approval import canonical_hash

from executor.engine import execute_action, execute_rollback
from executor.schemas import ExecutionRequest
from integrations.actions import rollback_action_for_action
from runtime.storage import (
    get_execution_by_id,
    log_audit_event,
    record_execution,
    finish_execution,
    record_rollback,
    update_latest_rollback_status,
)


def _claim(request: ExecutionRequest, operation: str) -> tuple[str, dict | None]:
    request_hash = canonical_hash({"operation": operation, "request": request.model_dump()})
    try:
        record_execution(request.execution_id, request.run_id, request.execution_mode, "", request.action,
                         {"status": "in_progress", "request_hash": request_hash})
    except IntegrityError:
        existing = get_execution_by_id(request.execution_id)
        if existing is None or existing.get("request_hash") != request_hash:
            raise ValueError("Execution ID already belongs to a different or legacy request")
        if existing.get("status") in {"in_progress", "uncertain"}:
            raise RuntimeError("Execution is in progress or its outcome is uncertain; inspect it before retrying")
        return request_hash, existing
    return request_hash, None


class ExecutionService:
    def execute(self, request: ExecutionRequest) -> Dict[str, Any]:
        request_hash, existing = _claim(request, "execute")
        if existing is not None:
            return existing

        try:
            result = execute_action(
                run_id=request.run_id,
                execution_mode=request.execution_mode,
                tool_mode=request.tool_mode,
                incident_id=request.incident_id,
                incident=request.incident,
                action=request.action,
                diagnosis=request.diagnosis,
                evidence_before=request.evidence_before,
            )
        except Exception as exc:
            finish_execution(request.execution_id, {"status": "uncertain", "request_hash": request_hash, "error": str(exc)})
            raise
        command = str(result.get("command", ""))
        rollback_command = request.rollback_commands[0] if request.rollback_commands else ""

        payload: Dict[str, Any] = {
            "request_hash": request_hash,
            "command": command,
            "execution_id": request.execution_id,
            "status": str(result.get("status", "completed")),
            "execution_results": result.get("execution_results", []),
            "evidence_after": result.get("evidence_after", {}),
            "improved": bool(result.get("improved", False)),
            "improvement_summary": str(result.get("improvement_summary", "")),
            "verification": result.get("verification", {}),
            "rollback_record": {
                "status": "ready" if rollback_command else "not_available",
                "rollback_command": rollback_command,
            },
            "executor_transport": "service",
        }

        if rollback_command:
            record_rollback(
                execution_id=request.execution_id,
                run_id=request.run_id,
                rollback_command=rollback_command,
                payload={
                    "action": request.action,
                    "command": rollback_command,
                    "rollback_action": rollback_action_for_action(request.action, request.incident),
                },
            )
        if request.run_id:
            log_audit_event(
                request.run_id,
                "execution_completed",
                {
                    "execution_id": request.execution_id,
                    "execution_mode": request.execution_mode,
                    "command": command,
                    "improved": payload["improved"],
                },
                actor="executor",
            )
        finish_execution(request.execution_id, payload)
        return payload

    def rollback(self, request: ExecutionRequest) -> Dict[str, Any]:
        request_hash, existing = _claim(request, "rollback")
        if existing is not None:
            return existing
        try:
            result = execute_rollback(
                run_id=request.run_id,
                execution_mode=request.execution_mode,
                tool_mode=request.tool_mode,
                incident_id=request.incident_id,
                incident=request.incident,
                action=request.action,
            )
        except Exception as exc:
            finish_execution(request.execution_id, {"status": "uncertain", "request_hash": request_hash, "error": str(exc)})
            raise
        if request.run_id:
            update_latest_rollback_status(
                request.run_id,
                status="executed" if result.get("execution_results") else result.get("status", "not_available"),
                payload={
                    "rollback_result": result,
                    "action": request.action,
                },
            )
            log_audit_event(
                request.run_id,
                "rollback_executed" if result.get("execution_results") else "rollback_not_executed",
                {
                    "execution_mode": request.execution_mode,
                    "command": result.get("command", ""),
                },
                actor="executor",
            )
        payload = {
            "request_hash": request_hash,
            "command": result.get("command", ""),
            "execution_id": request.execution_id,
            "status": str(result.get("status", "completed")),
            "execution_results": result.get("execution_results", []),
            "evidence_after": result.get("evidence_after", {}),
            "improved": bool(result.get("improved", False)),
            "improvement_summary": str(result.get("improvement_summary", "")),
            "verification": result.get("verification", {}),
            "rollback_record": {"status": "executed" if result.get("execution_results") else result.get("status", "not_available"), "rollback_command": result.get("command", "")},
            "executor_transport": "service",
        }
        finish_execution(request.execution_id, payload)
        return payload
