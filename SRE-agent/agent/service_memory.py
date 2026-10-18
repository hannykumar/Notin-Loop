from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from runtime.storage import get_infrastructure_memory, get_or_create_service_memory, list_integrations, list_operator_feedback


@lru_cache(maxsize=128)
def _cached_service_memory(service: str, namespace: str) -> Dict[str, Any]:
    payload = get_infrastructure_memory(service, namespace=namespace)
    if payload:
        enriched = dict(payload)
        memory_payload = dict(enriched.get("payload") or {})
        feedback = [
            item
            for item in list_operator_feedback(service=service, limit=25)
            if item.get("diagnosis_correct") is True and int(item.get("rating", 0) or 0) >= 4
        ]
        memory_payload["validated_feedback_priors"] = [
            {
                "incident_type": item.get("incident_snapshot", {}).get("incident_type", "Unknown"),
                "rating": item.get("rating"),
                "run_id": item.get("run_id"),
            }
            for item in feedback[:5]
        ]
        enriched["payload"] = memory_payload
        return enriched
    return {
        "service": service,
        "namespace": namespace,
        "summary": "",
        "payload": {},
        "source": "none",
    }


def load_service_memory(service: str, namespace: str = "prod") -> Dict[str, Any]:
    return dict(_cached_service_memory(str(service or "").strip(), str(namespace or "prod").strip() or "prod"))


def reset_service_memory_cache() -> None:
    _cached_service_memory.cache_clear()



def summarize_service_memory(service_memory: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(service_memory.get("payload") or {})
    owner = dict(payload.get("owner") or {})
    dashboards = list(payload.get("dashboards") or [])
    deploys = list(payload.get("recent_deploys") or [])
    history = list(payload.get("incident_history") or [])
    dependencies = list(payload.get("dependencies") or [])
    return {
        "service": service_memory.get("service", ""),
        "namespace": service_memory.get("namespace", ""),
        "summary": service_memory.get("summary", ""),
        "owner_team": owner.get("team", ""),
        "owner_slack": owner.get("slack", ""),
        "dashboard_titles": [item.get("title", "") for item in dashboards[:3]],
        "recent_deploy_versions": [item.get("version", "") for item in deploys[:3]],
        "recent_incident_types": [item.get("type", "") for item in history[:3]],
        "dependencies": dependencies,
    }



def enabled_integrations_summary() -> List[Dict[str, Any]]:
    rows = []
    for integration in list_integrations(limit=50):
        if not integration.get("enabled", False):
            continue
        rows.append(
            {
                "integration_id": integration.get("integration_id", ""),
                "name": integration.get("name", ""),
                "type": integration.get("integration_type", ""),
                "health_status": integration.get("health_status", "unknown"),
                "safety_level": integration.get("safety_level", "unknown"),
                "confirmation_mode": integration.get("confirmation_mode", "auto"),
                "product_role": integration.get("product_role", ""),
                "tools": list((integration.get("enabled_tools") or {}).get("tools", [])),
                "ingest_paths": list((integration.get("enabled_tools") or {}).get("ingest_paths", [])),
            }
        )
    return rows
