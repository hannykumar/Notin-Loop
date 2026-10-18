from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Literal
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.incident_catalog import catalog_version
from agent.approval import verify_proposal_artifact
from agent.incident_context import build_incident_context
from agent.langgraph_agent import load_incident, normalize_execution_mode, run_incident_langgraph
from executor.client import ExecutorClient
from integrations.mock_mcp import MockMCP
from integrations.incident_registry import list_mock_incidents
from runtime.alert_sources import is_grafana_meta_alert, normalize_alert_event, supported_alert_sources
from runtime.auth import Principal, auth_enabled, authorize_action, require_role, supported_roles
from runtime.db import init_db
from runtime.job_queue import get_job_queue
from runtime.job_runner import reset_mock_state_if_needed, run_execute_job, run_plan_job
from runtime.metrics import REQUEST_COUNTER, metrics_response
from runtime.rate_limit import enforce_rate_limit
from runtime.runtime_health import runtime_health_snapshot
from runtime.resilience import run_with_retry
from runtime.settings import get_settings
from runtime.evaluations import router as evaluation_router
from runtime.storage import (
    create_run,
    get_latest_rollback,
    get_infrastructure_memory,
    get_or_create_service_memory,
    get_run,
    list_audit_events,
    list_infrastructure_memory,
    list_incident_learning,
    list_operator_feedback,
    list_integrations,
    list_runs,
    list_service_memory,
    log_audit_event,
    mark_approval,
    mark_run_status,
    save_operator_feedback,
    update_execution_result,
    update_plan_result,
)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    settings = _settings()
    if settings.async_queue_enabled:
        get_job_queue().start()
    yield


app = FastAPI(title="self-healing-sre-runtime-api", lifespan=_lifespan)
app.include_router(evaluation_router)


class PlanRequest(BaseModel):
    incident_id: str = Field(..., description="Incident id, e.g. INC-001")
    execution_mode: Literal["preview", "simulate", "live"] = "preview"
    wait_for_completion: bool = True


class RunListResponse(BaseModel):
    runs: list[dict[str, Any]]


class ApproveExecutionRequest(BaseModel):
    execution_mode: Literal["preview", "simulate", "live"] | None = None
    proposal_hash: str | None = None


class OperatorFeedbackRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    diagnosis_correct: bool | None = None
    action_helpful: bool | None = None
    notes: str = Field(default="", max_length=2000)


def _settings():
    return get_settings()


def _actor(principal: Principal) -> str:
    return f"{principal.name} ({principal.role})"


def _requested_execution_mode(value: Any) -> str:
    if str(value).strip().lower() == "simulate":
        if _settings().mcp_backend != "mock":
            raise HTTPException(status_code=400, detail="Simulation requires the mock backend")
        return "simulate"
    normalized = normalize_execution_mode(value)
    return "live" if normalized == "live" else "preview"


def _wait_for_run_payload(run_id: str, field_name: str, timeout_seconds: float) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        payload = get_run(run_id)
        if payload is None:
            break
        if payload.get(field_name) is not None:
            return payload
        if str(payload.get("status", "")) in {"failed", "execution_failed"}:
            return payload
        time.sleep(0.25)
    payload = get_run(run_id)
    return payload or {"run_id": run_id, "status": "unknown"}


def _plan_group_key(request_payload: Dict[str, Any]) -> str:
    trigger = request_payload.get("trigger") or {}
    source = str(request_payload.get("source", "ui"))
    incident_id = str(request_payload.get("incident_id", ""))
    alertname = str(trigger.get("alertname", ""))
    starts_at = str(trigger.get("starts_at", ""))
    return "|".join(part for part in ["plan", source, incident_id, alertname, starts_at] if part)


def _execute_group_key(run_id: str) -> str:
    return f"execute|{run_id}"


def _create_plan_run(
    *,
    incident_id: str,
    actor: str,
    request_payload: Dict[str, Any],
    audit_context: Dict[str, Any] | None = None,
    wait_for_completion: bool = True,
    incident_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    run_id = f"run_{uuid4().hex[:10]}"
    reset_mock_state_if_needed(incident_id)
    create_run(run_id, request_payload)
    log_audit_event(run_id, "run_created", request_payload, actor=actor)
    if audit_context:
        log_audit_event(run_id, "run_triggered", audit_context, actor=actor)

    settings = _settings()
    if settings.async_queue_enabled:
        mark_run_status(run_id, "queued")
        submitted = get_job_queue().submit(
            "plan",
            run_id,
            _plan_group_key(request_payload),
            {
                "run_id": run_id,
                "incident_id": incident_id,
                "actor": actor,
                "incident_context": incident_context or {},
            },
        )
        if not submitted:
            mark_run_status(run_id, "duplicate")
            log_audit_event(run_id, "plan_deduplicated", {"group_key": _plan_group_key(request_payload)}, actor=actor)
        if wait_for_completion:
            payload = _wait_for_run_payload(run_id, "plan_result", settings.request_timeout_seconds)
            if str(payload.get("status", "")) == "failed":
                detail = ((payload.get("plan_result") or {}).get("error")) or "Plan job failed"
                raise HTTPException(status_code=500, detail=str(detail))
            return payload
        return get_run(run_id) or {"run_id": run_id, "status": "queued"}

    try:
        run_plan_job(run_id=run_id, incident_id=incident_id, actor=actor, incident_context=incident_context)
    except Exception as exc:
        mark_run_status(run_id, "failed")
        log_audit_event(run_id, "plan_failed", {"error": str(exc)}, actor=actor)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=500, detail=f"Run disappeared: {run_id}")
    return payload


def _parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _grafana_run_matches(run: Dict[str, Any], incident_id: str, alertname: str) -> bool:
    request_payload = run.get("request") or {}
    trigger = request_payload.get("trigger") or {}
    return (
        str(request_payload.get("source", "")) == "grafana_webhook"
        and str(request_payload.get("incident_id", "")) == incident_id
        and str(trigger.get("alertname", "")) == alertname
    )


def _cleanup_grafana_runs(incident_id: str, alertname: str, starts_at: str) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_settings().grafana_active_run_window_seconds)
    target_start = _parse_utc(starts_at)
    for run in list_runs(limit=200):
        if not _grafana_run_matches(run, incident_id, alertname):
            continue
        if str(run.get("status", "")) not in {"queued", "planning", "planned", "awaiting_approval", "approved"}:
            continue

        request_payload = run.get("request") or {}
        trigger = request_payload.get("trigger") or {}
        run_start_text = str(trigger.get("starts_at") or "")
        run_start = _parse_utc(run_start_text)
        created_at = _parse_utc(str(run.get("created_at") or ""))

        if starts_at and run_start_text and run_start_text != starts_at:
            mark_run_status(str(run.get("run_id", "")), "superseded")
            log_audit_event(
                str(run.get("run_id", "")),
                "grafana_run_superseded",
                {"alertname": alertname, "starts_at": starts_at},
                actor="grafana-webhook",
            )
            continue

        if target_start and run_start and run_start != target_start:
            mark_run_status(str(run.get("run_id", "")), "superseded")
            log_audit_event(
                str(run.get("run_id", "")),
                "grafana_run_superseded",
                {"alertname": alertname, "starts_at": starts_at},
                actor="grafana-webhook",
            )
            continue

        if created_at and created_at < cutoff:
            mark_run_status(str(run.get("run_id", "")), "expired")
            log_audit_event(
                str(run.get("run_id", "")),
                "grafana_run_expired",
                {"alertname": alertname, "cutoff": cutoff.isoformat()},
                actor="grafana-webhook",
            )


def _mark_grafana_runs_resolved(incident_id: str, alertname: str, starts_at: str) -> int:
    resolved_count = 0
    target_start = _parse_utc(starts_at)
    for run in list_runs(limit=200):
        if not _grafana_run_matches(run, incident_id, alertname):
            continue
        run_status = str(run.get("status", ""))
        if run_status in {"superseded", "expired"}:
            continue

        trigger = (run.get("request") or {}).get("trigger") or {}
        run_start_text = str(trigger.get("starts_at") or "")
        run_start = _parse_utc(run_start_text)
        if starts_at and run_start_text and run_start_text != starts_at:
            continue
        if target_start and run_start and run_start != target_start:
            continue

        run_id = str(run.get("run_id", ""))
        if run_status in {"queued", "planning", "planned", "awaiting_approval", "approved"}:
            mark_run_status(run_id, "resolved")
        already_recorded = any(
            event.get("event_type") == "grafana_alert_resolved"
            and str((event.get("payload") or {}).get("starts_at") or "") == starts_at
            for event in list_audit_events(run_id)
        )
        if not already_recorded:
            log_audit_event(
                run_id,
                "grafana_alert_resolved",
                {"alertname": alertname, "starts_at": starts_at, "run_status": run_status},
                actor="grafana-webhook",
            )
        resolved_count += 1
    return resolved_count


def _active_grafana_run(incident_id: str, alertname: str, starts_at: str) -> Dict[str, Any] | None:
    target_start = _parse_utc(starts_at)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_settings().grafana_active_run_window_seconds)
    for run in list_runs(limit=50):
        if not _grafana_run_matches(run, incident_id, alertname):
            continue
        if str(run.get("status", "")) in {"queued", "planning", "planned", "awaiting_approval", "approved"}:
            trigger = (run.get("request") or {}).get("trigger") or {}
            run_start_text = str(trigger.get("starts_at") or "")
            run_start = _parse_utc(run_start_text)
            if starts_at and run_start_text:
                if run_start_text == starts_at:
                    return run
                continue
            if target_start and run_start:
                if run_start == target_start:
                    return run
                continue
            created_at = _parse_utc(str(run.get("created_at") or ""))
            if created_at and created_at >= cutoff:
                return run
    return None


@app.get("/health")
def health() -> Dict[str, Any]:
    settings = _settings()
    runtime = runtime_health_snapshot(active_probe=False)
    return {
        "ok": True,
        "storage": settings.database_url.split(":", 1)[0],
        "executor": settings.executor_base_url or "embedded",
        "mcp_backend": settings.mcp_backend,
        "grafana_webhook_enabled": bool(settings.grafana_webhook_token),
        "alert_webhook_enabled": bool(settings.alert_webhook_token),
        "live_backend": runtime.get("live_backends", {}),
        "model_runtime": runtime.get("planner", {}),
        "auth_enabled": auth_enabled(),
        "roles": supported_roles(),
        "runtime_profile": {
            "product_name": "AI Assistant for SRE",
            "assistant_type": "hybrid_guarded_assistant",
            "tool_mode": "mcp",
            "default_execution_mode": "preview",
            "supported_execution_modes": ["preview", "live"],
            "planner_provider": settings.planner_provider,
            "planner_model": settings.planner_model,
            "catalog_version": catalog_version(),
            "async_queue_enabled": settings.async_queue_enabled,
            "queue_backend": settings.queue_backend,
            "max_async_workers": settings.max_async_workers,
            "integration_count": len(list_integrations(limit=100)),
            "alert_sources": supported_alert_sources(),
            "investigation_mode": "coordinator_plus_specialists",
        },
    }


@app.get("/metrics")
def get_metrics() -> Response:
    payload, content_type = metrics_response()
    return Response(content=payload, media_type=content_type)


@app.get("/service-memory")
def get_service_memory_index(limit: int = Query(default=50, ge=1, le=200), principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    return {"items": list_service_memory(limit=limit)}


@app.get("/service-memory/{service}")
def get_service_memory_detail(service: str, principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    memory = get_or_create_service_memory(service)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"Service memory not found: {service}")
    return memory


@app.get("/infrastructure-memory")
def get_infrastructure_memory_index(limit: int = Query(default=50, ge=1, le=200), principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    return {"items": list_infrastructure_memory(limit=limit)}


@app.get("/infrastructure-memory/{service}")
def get_infrastructure_memory_detail(service: str, principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    memory = get_infrastructure_memory(service)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"Infrastructure memory not found: {service}")
    return memory


@app.get("/incident-memory")
def get_incident_learning_index(
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    return {"items": list_incident_learning(limit=limit), "operator_feedback": list_operator_feedback(limit=limit)}


@app.post("/runs/{run_id}/feedback", dependencies=[Depends(enforce_rate_limit)])
def submit_operator_feedback(
    run_id: str,
    feedback: OperatorFeedbackRequest,
    principal: Principal = Depends(require_role("operator")),
) -> Dict[str, Any]:
    if get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    actor = _actor(principal)
    saved = save_operator_feedback(
        run_id,
        actor=actor,
        rating=feedback.rating,
        diagnosis_correct=feedback.diagnosis_correct,
        action_helpful=feedback.action_helpful,
        notes=feedback.notes.strip(),
    )
    from agent.service_memory import reset_service_memory_cache

    reset_service_memory_cache()
    log_audit_event(run_id, "operator_feedback_recorded", {"rating": feedback.rating}, actor=actor)
    return {"feedback": saved}


@app.get("/integrations")
def get_integrations(principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    return {"items": list_integrations(limit=100)}


@app.get("/alert-sources")
def get_alert_sources(principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    return {"items": supported_alert_sources()}


@app.get("/platform/runtime-health")
def get_runtime_health(
    active_probe: bool = Query(default=False),
    principal: Principal = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    return runtime_health_snapshot(active_probe=active_probe)


@app.get("/platform/overview")
def get_platform_overview(principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    integrations = list_integrations(limit=100)
    infrastructure_memory = list_infrastructure_memory(limit=200)
    runtime = runtime_health_snapshot(active_probe=False)
    return {
        "product_name": "AI Assistant for SRE",
        "assistant_type": "hybrid_guarded_assistant",
        "integrations": integrations,
        "alert_sources": supported_alert_sources(),
        "infrastructure_memory_count": len(infrastructure_memory),
        "healthy_integrations": sum(1 for item in integrations if str(item.get("health_status", "")).lower() in {"ready", "healthy"}),
        "manual_confirmation_integrations": [
            item.get("integration_id", "")
            for item in integrations
            if str(item.get("confirmation_mode", "auto")).lower() == "manual"
        ],
        "runtime_health": runtime,
    }


@app.get("/runs", response_model=RunListResponse)
def get_runs(limit: int = Query(default=20, ge=1, le=200), principal: Principal = Depends(require_role("viewer"))) -> RunListResponse:
    return RunListResponse(runs=list_runs(limit=limit))


@app.post("/runs/plan", dependencies=[Depends(enforce_rate_limit)])
def create_plan(req: PlanRequest, principal: Principal = Depends(require_role("operator"))) -> Dict[str, Any]:
    actor = _actor(principal)
    execution_mode = _requested_execution_mode(req.execution_mode)
    request_payload = {
        "incident_id": req.incident_id,
        "execution_mode": execution_mode,
        "tool_mode": "mcp",
        "source": "ui",
    }
    payload = _create_plan_run(
        incident_id=req.incident_id,
        actor=actor,
        request_payload=request_payload,
        audit_context={"source": "ui"},
        wait_for_completion=req.wait_for_completion,
    )
    REQUEST_COUNTER.labels(endpoint="runs_plan", outcome="accepted").inc()
    return payload


@app.get("/scenarios")
def get_scenarios(principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    return {"items": list_mock_incidents(), "backend": _settings().mcp_backend}


_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


@app.get("/", include_in_schema=False)
def workbench() -> FileResponse:
    return FileResponse(_FRONTEND / "index.html")


app.mount("/assets", StaticFiles(directory=_FRONTEND, check_dir=False), name="frontend")


@app.post("/alerts/grafana/webhook")
def ingest_grafana_alert(
    token: str = Query(default="", description="Shared token for Grafana webhook authentication"),
    payload: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    settings = _settings()
    if settings.grafana_webhook_token and token != settings.grafana_webhook_token:
        raise HTTPException(status_code=401, detail="Invalid Grafana webhook token")

    normalized = normalize_alert_event("grafana", payload)
    if is_grafana_meta_alert(normalized):
        REQUEST_COUNTER.labels(endpoint="grafana_webhook", outcome="observability_meta_alert").inc()
        return {
            "accepted": True,
            "created": False,
            "reason": "observability_meta_alert",
            "normalized": normalized,
        }
    incident_context = build_incident_context(normalized, payload)
    if normalized["status"] == "resolved":
        resolved_count = _mark_grafana_runs_resolved(
            normalized["incident_id"],
            normalized["alertname"],
            str(normalized.get("starts_at", "")),
        )
        return {
            "accepted": True,
            "created": False,
            "reason": "resolved_alert",
            "normalized": normalized,
            "resolved_runs": resolved_count,
        }

    _cleanup_grafana_runs(normalized["incident_id"], normalized["alertname"], str(normalized.get("starts_at", "")))
    active_run = _active_grafana_run(normalized["incident_id"], normalized["alertname"], str(normalized.get("starts_at", "")))
    if active_run is not None:
        return {
            "accepted": True,
            "created": False,
            "reason": "duplicate_alert",
            "normalized": normalized,
            "active_run_id": active_run.get("run_id"),
        }

    actor = "grafana-webhook"
    request_payload = {
        "incident_id": normalized["incident_id"],
        "execution_mode": "preview",
        "tool_mode": "mcp",
        "source": "grafana_webhook",
        "incident_context": incident_context,
        "trigger": {
            "provider": "grafana",
            "scenario": normalized["scenario"],
            "alertname": normalized["alertname"],
            "summary": normalized["summary"],
            "fingerprint": normalized.get("fingerprint", ""),
            "starts_at": normalized.get("starts_at", ""),
            "labels": normalized["labels"],
        },
    }
    result = _create_plan_run(
        incident_id=normalized["incident_id"],
        actor=actor,
        request_payload=request_payload,
        audit_context={"source": "grafana_webhook", "normalized": normalized},
        wait_for_completion=settings.webhook_wait_for_plan,
        incident_context=incident_context,
    )
    REQUEST_COUNTER.labels(endpoint="grafana_webhook", outcome="created").inc()
    return {
        "accepted": True,
        "created": True,
        "run_id": result["run_id"],
        "incident_id": normalized["incident_id"],
        "normalized": normalized,
        "run": result,
    }


@app.post("/alerts/events")
def ingest_generic_alert(
    token: str = Query(default="", description="Shared token for generic alert webhook authentication"),
    payload: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    settings = _settings()
    if settings.alert_webhook_token and token != settings.alert_webhook_token:
        raise HTTPException(status_code=401, detail="Invalid alert webhook token")

    normalized = normalize_alert_event("generic", payload)
    incident_context = build_incident_context(normalized, payload)
    if normalized["status"] == "resolved":
        return {
            "accepted": True,
            "created": False,
            "reason": "resolved_alert",
            "normalized": normalized,
            "resolved_runs": 0,
        }

    actor = "generic-alert-webhook"
    request_payload = {
        "incident_id": normalized["incident_id"],
        "execution_mode": "preview",
        "tool_mode": "mcp",
        "source": "generic_alert",
        "incident_context": incident_context,
        "trigger": {
            "provider": "generic",
            "scenario": normalized["scenario"],
            "alertname": normalized["alertname"],
            "summary": normalized["summary"],
            "fingerprint": normalized.get("fingerprint", ""),
            "starts_at": normalized.get("starts_at", ""),
            "labels": normalized["labels"],
        },
    }
    result = _create_plan_run(
        incident_id=normalized["incident_id"],
        actor=actor,
        request_payload=request_payload,
        audit_context={"source": "generic_alert", "normalized": normalized},
        wait_for_completion=settings.webhook_wait_for_plan,
        incident_context=incident_context,
    )
    REQUEST_COUNTER.labels(endpoint="generic_alert", outcome="created").inc()
    return {
        "accepted": True,
        "created": True,
        "run_id": result["run_id"],
        "incident_id": normalized["incident_id"],
        "normalized": normalized,
        "run": result,
    }


@app.post("/runs/{run_id}/approve-execute", dependencies=[Depends(enforce_rate_limit)])
def approve_execute(
    run_id: str,
    req_body: ApproveExecutionRequest = Body(default_factory=ApproveExecutionRequest),
    principal: Principal = Depends(require_role("approver")),
) -> Dict[str, Any]:
    actor = _actor(principal)
    payload = get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    approval = payload.get("approval") or {}
    artifact = approval.get("artifact") or {}
    if payload.get("status") != "awaiting_approval" or approval.get("status") != "pending":
        raise HTTPException(status_code=409, detail="This run has no pending approval")
    if req_body.proposal_hash is not None and req_body.proposal_hash != artifact.get("proposal_hash"):
        raise HTTPException(status_code=409, detail="The displayed proposal does not match the stored proposal")
    try:
        verify_proposal_artifact(artifact, payload.get("plan_result") or {})
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    req = payload.get("request", {})
    requested_mode = req_body.execution_mode if req_body.execution_mode is not None else req.get("execution_mode", "preview")
    execution_mode = _requested_execution_mode(requested_mode)
    planned_result = payload.get("plan_result") or {}
    authorize_action(principal, planned_result.get("proposed_action") or {})
    try:
        approved_payload = mark_approval(run_id, approver=actor, status="approved", execution_mode=execution_mode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if str(req.get("execution_mode", "preview")) != execution_mode:
        log_audit_event(
            run_id,
            "execution_mode_overridden",
            {"actor": actor, "execution_mode": execution_mode},
            actor=actor,
        )

    approved_artifact = dict((approved_payload.get("approval") or {}).get("artifact") or {})
    log_audit_event(
        run_id,
        "approval_granted",
        {
            "actor": actor,
            "proposal_id": approved_artifact.get("proposal_id", ""),
            "proposal_hash": approved_artifact.get("proposal_hash", ""),
        },
        actor=actor,
    )

    try:
        settings = _settings()
        if settings.async_queue_enabled:
            mark_run_status(run_id, "execution_queued")
            submitted = get_job_queue().submit(
                "execute",
                run_id,
                _execute_group_key(run_id),
                {
                    "run_id": run_id,
                    "incident_id": str(req.get("incident_id", "")),
                    "execution_mode": execution_mode,
                    "tool_mode": str(req.get("tool_mode", "mcp")),
                    "actor": actor,
                    "incident_context": dict(req.get("incident_context") or {}),
                },
            )
            if not submitted:
                log_audit_event(run_id, "execution_deduplicated", {"run_id": run_id}, actor=actor)
            payload = _wait_for_run_payload(run_id, "execute_result", settings.execution_request_timeout_seconds)
            if str(payload.get("status", "")) == "execution_failed":
                detail = ((payload.get("execute_result") or {}).get("error")) or "Execution job failed"
                raise HTTPException(status_code=500, detail=str(detail))
            REQUEST_COUNTER.labels(endpoint="approve_execute", outcome="accepted").inc()
            return payload

        run_execute_job(
            run_id=run_id,
            incident_id=str(req.get("incident_id", "")),
            execution_mode=execution_mode,
            tool_mode=str(req.get("tool_mode", "mcp")),
            actor=actor,
            incident_context=dict(req.get("incident_context") or {}),
        )
    except Exception as exc:
        mark_run_status(run_id, "execution_failed")
        log_audit_event(run_id, "execution_failed", {"error": str(exc)}, actor=actor)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=500, detail=f"Run disappeared: {run_id}")
    REQUEST_COUNTER.labels(endpoint="approve_execute", outcome="accepted").inc()
    return payload


@app.get("/runs/{run_id}")
def get_run_details(run_id: str, principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    payload = get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return payload


@app.get("/runs/{run_id}/trace")
def get_run_trace(run_id: str, principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    payload = get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    latest = payload.get("execute_result") or payload.get("plan_result") or {}
    trace_path = Path(str(latest.get("saved_trace", "")))
    if not trace_path.is_file():
        raise HTTPException(status_code=404, detail=f"Trace file not found: {trace_path}")
    return {
        "run_id": run_id,
        "trace_path": str(trace_path),
        "trace": json.loads(trace_path.read_text(encoding="utf-8")),
    }


@app.get("/runs/{run_id}/audit")
def get_run_audit(run_id: str, principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    if get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {"run_id": run_id, "events": list_audit_events(run_id)}


@app.get("/runs/{run_id}/rollback")
def get_run_rollback(run_id: str, principal: Principal = Depends(require_role("viewer"))) -> Dict[str, Any]:
    if get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    rollback = get_latest_rollback(run_id)
    if rollback is None:
        raise HTTPException(status_code=404, detail=f"Rollback record not found for run: {run_id}")
    return {"run_id": run_id, "rollback": rollback}


@app.post("/runs/{run_id}/rollback-execute", dependencies=[Depends(enforce_rate_limit)])
def execute_rollback(run_id: str, principal: Principal = Depends(require_role("admin"))) -> Dict[str, Any]:
    payload = get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    rollback = get_latest_rollback(run_id)
    if rollback is None:
        raise HTTPException(status_code=404, detail=f"Rollback record not found for run: {run_id}")

    rollback_action = (rollback.get("payload") or {}).get("rollback_action")
    if not rollback_action:
        raise HTTPException(status_code=400, detail="Rollback is not executable for this run")

    req = payload.get("request", {})
    incident = dict((payload.get("plan_result") or {}).get("incident") or {})
    if not incident:
        raise HTTPException(status_code=409, detail="Original incident snapshot is unavailable")
    actor = _actor(principal)
    log_audit_event(run_id, "rollback_requested", {"actor": actor}, actor=actor)

    try:
        result = run_with_retry(
            lambda: ExecutorClient().rollback(
                {
                    "execution_id": f"rollback_{uuid4().hex[:10]}",
                    "run_id": run_id,
                    "incident_id": str(req.get("incident_id", "")),
                    "incident": incident,
                    "action": (rollback.get("payload") or {}).get("action") or (payload.get("plan_result") or {}).get("proposed_action"),
                    "execution_mode": str(req.get("execution_mode", "preview")),
                    "tool_mode": str(req.get("tool_mode", "mcp")),
                }
            ),
            retries=0,
            timeout_seconds=_settings().request_timeout_seconds,
            operation_name="rollback_run",
        )
    except Exception as exc:
        log_audit_event(run_id, "rollback_failed", {"error": str(exc)}, actor=actor)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    log_audit_event(
        run_id,
        "rollback_completed",
        {"command": result.get("rollback_record", {}).get("rollback_command", "")},
        actor=actor,
    )
    return {"run_id": run_id, "rollback_result": result}
