"""SRE plugin: one JSON log line per request and Prometheus /metrics.

Install with two lines in main.py:
    from app.sre_plugin import install
    install(app)

Signal format: factory_flow/SIGNALS.md (agreed with Track A).

Settings (read per request, so tests can override them):
    SRE_INSTANCE   "stable" or "canary" (default "stable")
    SRE_LOG_PATH   log file (default <factory_flow>/runs/server.log)
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Patterned on SRE-agent/lab/demo_service.py @ b4f7e0c (REQUESTS counter + _record helper + generate_latest).
REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled by the target service.",
    ["instance", "route", "method", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency in seconds.",
    ["instance", "route"],
)

DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / "runs" / "server.log"
_LOG_LOCK = threading.Lock()
_UNLOGGED_PATHS = {"/metrics"}


def instance_name() -> str:
    return os.getenv("SRE_INSTANCE", "stable")


def log_path() -> Path:
    return Path(os.getenv("SRE_LOG_PATH") or DEFAULT_LOG_PATH)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def _record(instance: str, route: str, method: str, status: int, latency_ms: float) -> None:
    REQUESTS.labels(instance=instance, route=route, method=method, status=str(status)).inc()
    LATENCY.labels(instance=instance, route=route).observe(latency_ms / 1000)


def _write_line(entry: dict[str, Any]) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True) + "\n"
    with _LOG_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def sre_request_log(request: Request, call_next: Any) -> Response:
        if request.url.path in _UNLOGGED_PATHS:
            return await call_next(request)

        started = time.perf_counter()
        exception_type = None
        trace = None
        try:
            response = await call_next(request)
        except Exception as exc:  # the request failed with an unhandled exception -> 500
            exception_type = type(exc).__name__
            trace = traceback.format_exc()
            response = JSONResponse({"detail": "internal server error"}, status_code=500)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        instance = instance_name()
        route = _route_template(request)
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "instance": instance,
            "method": request.method,
            "path": request.url.path,
            "route": route,
            "query": dict(request.query_params),
            "status": response.status_code,
            "latency_ms": latency_ms,
        }
        if response.status_code >= 500 and exception_type:
            entry["exception_type"] = exception_type
            entry["traceback"] = trace
        _record(instance, route, request.method, response.status_code, latency_ms)
        _write_line(entry)
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
