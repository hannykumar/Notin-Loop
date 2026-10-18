from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict

from integrations.live_backends import LiveSREBackend
from runtime.settings import get_settings


LIVE_TOOLS: dict[str, Callable[[LiveSREBackend, str, str], Any]] = {
    "get_metrics": lambda backend, service, namespace: backend.get_metrics(service=service, namespace=namespace),
    "query_prometheus": lambda backend, service, namespace: backend.query_prometheus(
        f'service_health_score{{service="{service}",namespace="{namespace}"}}'
    ),
    "query_loki": lambda backend, service, namespace: backend.query_loki(service=service, namespace=namespace, limit=20),
    "query_tempo": lambda backend, service, namespace: backend.query_tempo(service=service, namespace=namespace, limit=10),
    "get_dashboard_context": lambda backend, service, namespace: backend.get_dashboard_context(service=service, namespace=namespace),
    "get_recent_deploys": lambda backend, service, namespace: backend.get_recent_deploys(service=service, namespace=namespace),
    "get_service_owner": lambda backend, service, namespace: backend.get_service_owner(service=service),
    "get_incident_history": lambda backend, service, namespace: backend.get_incident_history(service=service, limit=5),
}


def live_backend_requirements() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "prometheus_configured": bool(settings.prometheus_base_url),
        "loki_configured": bool(settings.loki_base_url) if settings.logs_backend == "loki" else True,
        "kubeconfig_present": bool(settings.kubeconfig_path),
        "logs_backend": settings.logs_backend,
    }


def run_live_backend_probe(service: str, namespace: str) -> Dict[str, Any]:
    backend = LiveSREBackend()
    results: Dict[str, Any] = {
        "service": service,
        "namespace": namespace,
        "requirements": live_backend_requirements(),
        "tools": {},
    }
    for tool_name, handler in LIVE_TOOLS.items():
        started = time.perf_counter()
        try:
            payload = handler(backend, service, namespace)
            results["tools"][tool_name] = {
                "ok": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "payload": payload,
            }
        except Exception as exc:  # pragma: no cover - depends on external infra
            results["tools"][tool_name] = {
                "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "error": str(exc),
            }
    results["healthy_tools"] = sum(1 for item in results["tools"].values() if item.get("ok"))
    results["total_tools"] = len(results["tools"])
    return results


def main() -> None:
    settings = get_settings()
    service = (settings.live_eval_service if hasattr(settings, "live_eval_service") else "") or "api"
    namespace = (settings.live_eval_namespace if hasattr(settings, "live_eval_namespace") else "") or "prod"
    print(json.dumps(run_live_backend_probe(service=service, namespace=namespace), indent=2))


if __name__ == "__main__":
    main()
