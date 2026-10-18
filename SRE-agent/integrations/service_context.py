from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

SERVICE_CONTEXT_PATH = Path("config/service_context.json")


@lru_cache(maxsize=1)
def load_service_context() -> Dict[str, Any]:
    return json.loads(SERVICE_CONTEXT_PATH.read_text(encoding="utf-8"))


def service_context(service: str) -> Dict[str, Any]:
    services = load_service_context().get("services", {})
    return dict(services.get(str(service or ""), {}))


def recent_deploys(service: str) -> List[Dict[str, Any]]:
    return list(service_context(service).get("recent_deploys", []))


def dashboards(service: str) -> List[Dict[str, Any]]:
    return list(service_context(service).get("dashboards", []))


def incident_history(service: str) -> List[Dict[str, Any]]:
    return list(service_context(service).get("incident_history", []))


def owner(service: str) -> Dict[str, Any]:
    return dict(service_context(service).get("owner", {}))


def trace_summary(service: str) -> List[Dict[str, Any]]:
    return list(service_context(service).get("trace_summary", []))
