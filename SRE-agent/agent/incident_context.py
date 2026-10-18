from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import BaseModel, Field


class IncidentContext(BaseModel):
    """Provider-neutral alert context preserved for the entire incident run."""

    incident_id: str
    source: str
    alert_name: str
    status: str = "firing"
    service: str = "unknown-service"
    namespace: str = "default"
    environment: str = "unknown"
    severity: str = "unknown"
    started_at: str = ""
    received_at: str
    summary: str
    fingerprint: str = ""
    dashboard_url: str = ""
    labels: Dict[str, Any] = Field(default_factory=dict)
    annotations: Dict[str, Any] = Field(default_factory=dict)
    raw_alert: Dict[str, Any] = Field(default_factory=dict)


def build_incident_context(normalized: Dict[str, Any], raw_alert: Dict[str, Any]) -> Dict[str, Any]:
    labels = dict(normalized.get("labels") or {})
    annotations = dict(normalized.get("annotations") or {})
    source = str(normalized.get("source_type") or "generic")
    dashboard_url = str(
        annotations.get("dashboard_url")
        or annotations.get("dashboardURL")
        or raw_alert.get("dashboardURL")
        or raw_alert.get("externalURL")
        or ""
    )
    context = IncidentContext(
        incident_id=str(normalized.get("incident_id") or "UNKNOWN"),
        source=source,
        alert_name=str(normalized.get("alertname") or "unnamed-alert"),
        status=str(normalized.get("status") or "firing"),
        service=str(labels.get("service") or raw_alert.get("service") or "unknown-service"),
        namespace=str(labels.get("namespace") or raw_alert.get("namespace") or "default"),
        environment=str(labels.get("environment") or labels.get("env") or raw_alert.get("environment") or "unknown"),
        severity=str(labels.get("severity") or raw_alert.get("severity") or "unknown"),
        started_at=str(normalized.get("starts_at") or ""),
        received_at=datetime.now(timezone.utc).isoformat(),
        summary=str(normalized.get("summary") or normalized.get("alertname") or "Alert received"),
        fingerprint=str(normalized.get("fingerprint") or ""),
        dashboard_url=dashboard_url,
        labels=labels,
        annotations=annotations,
        raw_alert=dict(raw_alert),
    )
    return context.model_dump(mode="json")


def incident_payload_from_context(
    context: Dict[str, Any],
    fixture: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Overlay real alert metadata on fixture evidence, or create safe empty evidence."""

    service = str(context.get("service") or "unknown-service")
    namespace = str(context.get("namespace") or "default")
    incident_id = str(context.get("incident_id") or "UNKNOWN")
    payload: Dict[str, Any] = dict(fixture or {})
    payload.update(
        {
            "incident_id": incident_id,
            "title": str(context.get("alert_name") or payload.get("title") or "External alert"),
            "description": str(context.get("summary") or payload.get("description") or "Alert received"),
            "service": service,
            "namespace": namespace,
            "severity": str(context.get("severity") or payload.get("severity") or "unknown"),
            "environment": str(context.get("environment") or payload.get("environment") or "unknown"),
            "incident_context": dict(context),
        }
    )
    labels = dict(context.get("labels") or {})
    for key in ("config_management", "current_version", "previous_version", "gitops_manifest_path"):
        if labels.get(key) not in (None, ""):
            payload[key] = labels[key]
    payload.setdefault(
        "pod_status",
        {"pod_name": f"{service}-unknown", "phase": "Unknown", "reason": "Unknown", "restarts": 0, "replicas": 0},
    )
    payload.setdefault("pod_describe", {})
    payload.setdefault("metrics", {})
    payload.setdefault("logs_tail", [])
    payload.setdefault("cluster_events", [])
    return payload
