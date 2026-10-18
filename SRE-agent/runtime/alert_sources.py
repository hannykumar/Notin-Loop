from __future__ import annotations

from hashlib import sha256
from typing import Any, Dict, List


GRAFANA_META_ALERTS = {"datasourceerror", "datasourcenodata", "error", "nodata"}


def is_grafana_meta_alert(normalized: Dict[str, Any]) -> bool:
    """Return true for Grafana rule-health signals that are not service incidents."""
    return str(normalized.get("alertname") or "").strip().lower() in GRAFANA_META_ALERTS

def supported_alert_sources() -> List[Dict[str, Any]]:
    return [
        {
            "source_type": "grafana",
            "name": "Grafana",
            "ingest_path": "/alerts/grafana/webhook",
            "description": "Grafana-managed alert webhook adapter for the observability lab and external Grafana instances.",
            "demo_ready": True,
            "status_handling": ["firing", "resolved"],
        },
        {
            "source_type": "generic",
            "name": "Generic Webhook",
            "ingest_path": "/alerts/events",
            "description": "Platform-agnostic alert webhook adapter for external monitoring systems and future Nagios-style events.",
            "demo_ready": True,
            "status_handling": ["firing", "resolved"],
        },
    ]


def _select_alert_source(payload: Dict[str, Any]) -> Dict[str, Any]:
    alerts = payload.get("alerts") or []
    firing = [alert for alert in alerts if str(alert.get("status", "firing")).lower() == "firing"]
    if firing:
        return firing[0]
    if alerts:
        return alerts[0]
    return payload


def _infer_scenario(alertname: str, summary: str, labels: Dict[str, Any]) -> str:
    combined_text = " ".join(
        [
            alertname,
            summary,
            str(labels.get("service", "")),
            str(labels.get("severity", "")),
            str(labels.get("scenario", "")),
        ]
    ).lower()
    label_scenario = str(labels.get("scenario") or "").strip().lower()

    if label_scenario:
        return label_scenario
    if any(token in combined_text for token in ("5xx", "503", "upstream", "demo-api")):
        return "demo_service_high_5xx"
    if any(token in combined_text for token in ("crashloop", "oom", "oomkilled")):
        return "demo_service_crashloop"
    if any(token in combined_text for token in ("dns", "nxdomain", "resolve")):
        return "demo_service_dns_failure"
    return "unknown_alert"


def _generated_incident_id(*parts: Any) -> str:
    fingerprint = "|".join(str(part or "").strip().lower() for part in parts)
    return f"ALERT-{sha256(fingerprint.encode('utf-8')).hexdigest()[:12].upper()}"


def normalize_grafana_alert(payload: Dict[str, Any]) -> Dict[str, Any]:
    selected = _select_alert_source(payload)
    labels = dict(payload.get("commonLabels") or {})
    labels.update(selected.get("labels") or {})
    annotations = dict(payload.get("commonAnnotations") or {})
    annotations.update(selected.get("annotations") or {})

    alertname = str(labels.get("alertname") or payload.get("title") or payload.get("ruleName") or "grafana-alert").strip()
    summary = str(
        annotations.get("summary")
        or annotations.get("description")
        or payload.get("message")
        or payload.get("title")
        or alertname
    ).strip()
    starts_at = str(selected.get("startsAt") or "")
    fingerprint = str(selected.get("fingerprint") or "")
    incident_id = str(labels.get("incident_id") or "").strip().upper()
    if not incident_id:
        incident_id = _generated_incident_id("grafana", alertname, labels.get("service"), starts_at, fingerprint)
    scenario = _infer_scenario(alertname, summary, labels)
    return {
        "source_type": "grafana",
        "incident_id": incident_id,
        "scenario": scenario,
        "alertname": alertname,
        "summary": summary,
        "status": str(payload.get("status") or selected.get("status") or "firing").lower(),
        "fingerprint": fingerprint,
        "starts_at": starts_at,
        "labels": labels,
        "annotations": annotations,
        "source": selected,
    }


def normalize_generic_alert(payload: Dict[str, Any]) -> Dict[str, Any]:
    selected = dict(payload.get("alert") or payload)
    labels = dict(selected.get("labels") or {})
    annotations = dict(selected.get("annotations") or {})

    alertname = str(selected.get("alertname") or payload.get("title") or labels.get("alertname") or "generic-alert").strip()
    summary = str(
        selected.get("summary")
        or annotations.get("summary")
        or payload.get("summary")
        or payload.get("message")
        or alertname
    ).strip()
    if payload.get("service") and "service" not in labels:
        labels["service"] = payload.get("service")
    if payload.get("severity") and "severity" not in labels:
        labels["severity"] = payload.get("severity")

    starts_at = str(payload.get("starts_at") or selected.get("starts_at") or "")
    fingerprint = str(payload.get("fingerprint") or selected.get("fingerprint") or "")
    incident_id = str(payload.get("incident_id") or labels.get("incident_id") or "").strip().upper()
    if not incident_id:
        incident_id = _generated_incident_id("generic", alertname, labels.get("service"), starts_at, fingerprint)
    scenario = _infer_scenario(alertname, summary, labels)
    return {
        "source_type": "generic",
        "incident_id": incident_id,
        "scenario": str(payload.get("scenario") or scenario).strip().lower() or "generic_alert",
        "alertname": alertname,
        "summary": summary,
        "status": str(payload.get("status") or selected.get("status") or "firing").lower(),
        "fingerprint": fingerprint,
        "starts_at": starts_at,
        "labels": labels,
        "annotations": annotations,
        "source": selected,
    }


def normalize_alert_event(source_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized_type = str(source_type or "generic").strip().lower()
    if normalized_type == "grafana":
        return normalize_grafana_alert(payload)
    if normalized_type == "generic":
        return normalize_generic_alert(payload)
    raise ValueError(f"Unsupported alert source: {source_type}")
