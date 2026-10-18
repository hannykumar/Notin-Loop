from __future__ import annotations

from typing import Any, Dict

from agent.langgraph_agent import run_incident_langgraph
from agent.approval import verify_proposal_artifact
from agent.run_summaries import build_verification_summary
from executor.client import ExecutorClient
from integrations.actions import render_action_to_command
from integrations.mock_mcp import MockMCP
from runtime.resilience import run_with_retry
from runtime.settings import get_settings
from runtime.storage import get_run, log_audit_event, mark_run_status, update_execution_result, update_plan_result


def reset_mock_state_if_needed(incident_id: str) -> None:
    if get_settings().mcp_backend != "mock":
        return
    MockMCP.reset_live_state(incident_id)


def run_plan_job(*, run_id: str, incident_id: str, actor: str, incident_context: Dict[str, Any] | None = None) -> None:
    mark_run_status(run_id, "planning")
    try:
        result = run_with_retry(
            lambda: run_incident_langgraph(
                incident_id=incident_id,
                approved=False,
                execution_mode="preview",
                tool_mode="mcp",
                run_id=run_id,
                incident_context=incident_context,
                save_artifacts_enabled=True,
            ),
            retries=0,
            timeout_seconds=get_settings().investigation_timeout_seconds,
            operation_name="plan_run",
        )
    except Exception as exc:
        mark_run_status(run_id, "failed")
        log_audit_event(run_id, "plan_failed", {"error": str(exc)}, actor=actor)
        return

    update_plan_result(run_id, result)
    log_audit_event(
        run_id,
        "plan_completed",
        {
            "diagnosis": result.get("diagnosis"),
            "confirmed": result.get("confirmed"),
            "requires_human_approval": result.get("requires_human_approval"),
            "latency_metrics": result.get("latency_metrics", {}),
            "retrieval_quality": result.get("retrieval_quality", {}),
        },
        actor=actor,
    )


def run_execute_job(
    *,
    run_id: str,
    incident_id: str,
    execution_mode: str,
    tool_mode: str,
    actor: str,
    incident_context: Dict[str, Any] | None = None,
) -> None:
    mark_run_status(run_id, "executing")
    try:
        stored_run = get_run(run_id) or {}
        plan_result = dict(stored_run.get("plan_result") or {})
        approval = dict(stored_run.get("approval") or {})
        artifact = dict(approval.get("artifact") or {})
        if approval.get("status") != "approved":
            raise PermissionError("Execution requires an approved proposal")
        verify_proposal_artifact(artifact, plan_result)
        action = dict(artifact.get("action") or {})
        incident = dict(plan_result.get("incident") or {})
        if not incident:
            raise ValueError("The approved plan does not contain its incident snapshot")
        command = render_action_to_command(action, incident)
        if execution_mode == "preview":
            execution_payload = {
                "status": "preview",
                "execution_results": [{"status": "preview", "tool": "execute_remediation", "command": command, "action": action}],
                "evidence_after": {},
                "verification": {},
                "improved": False,
                "improvement_summary": "Preview only. No changes were applied.",
                "executor_transport": "none",
            }
        else:
            execution_payload = run_with_retry(
                lambda: ExecutorClient().execute(
                    {
                        "execution_id": f"{run_id}_approved",
                        "run_id": run_id,
                        "incident_id": incident_id,
                        "incident": incident,
                        "action": action,
                        "diagnosis": artifact.get("diagnosis", "Unknown"),
                        "execution_mode": execution_mode,
                        "tool_mode": tool_mode,
                        "evidence_before": plan_result.get("evidence", {}),
                        "rollback_commands": artifact.get("rollback_plan", []),
                    }
                ),
                retries=0,
                timeout_seconds=get_settings().execution_request_timeout_seconds,
                operation_name="execute_approved_proposal",
            )
        result = {
            **plan_result,
            **execution_payload,
            "execution_mode": execution_mode,
            "approved_proposal": artifact,
            "executed_proposal_hash": artifact.get("proposal_hash", ""),
            "proposed_action": action,
            "rollback_record": {
                "status": "ready" if artifact.get("rollback_plan") else "not_available",
                "rollback_command": (artifact.get("rollback_plan") or [""])[0],
            },
        }
        verification = dict(execution_payload.get("verification") or {})
        result["verification_outcome"] = str(
            verification.get("status")
            or ("resolved" if verification.get("resolved") else "not_run")
        ).strip().lower()
        result["verification_summary"] = build_verification_summary(result)
    except Exception as exc:
        mark_run_status(run_id, "execution_failed")
        log_audit_event(run_id, "execution_failed", {"error": str(exc)}, actor=actor)
        return

    mark_run_status(run_id, "verifying")
    update_execution_result(run_id, result)
    log_audit_event(
        run_id,
        "execution_result_persisted",
        {
            "executor_transport": result.get("executor_transport", ""),
            "improved": result.get("improved", False),
            "verification": result.get("verification", {}),
            "executed_proposal_hash": result.get("executed_proposal_hash", ""),
            "latency_metrics": result.get("latency_metrics", {}),
        },
        actor=actor,
    )


def dispatch_job(job: Dict[str, Any]) -> None:
    job_type = str(job.get("job_type", ""))
    payload = dict(job.get("payload") or {})
    if job_type == "plan":
        run_plan_job(
            run_id=str(payload["run_id"]),
            incident_id=str(payload["incident_id"]),
            actor=str(payload.get("actor", "system")),
            incident_context=dict(payload.get("incident_context") or {}),
        )
        return
    if job_type == "execute":
        run_execute_job(
            run_id=str(payload["run_id"]),
            incident_id=str(payload["incident_id"]),
            execution_mode=str(payload.get("execution_mode", "preview")),
            tool_mode=str(payload.get("tool_mode", "mcp")),
            actor=str(payload.get("actor", "system")),
            incident_context=dict(payload.get("incident_context") or {}),
        )
        return
    raise ValueError(f"Unknown job type: {job_type}")
