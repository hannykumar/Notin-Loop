from __future__ import annotations

import logging
import os
import socket
import time
import urllib.request
from typing import Any, Dict, Literal

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest


app = FastAPI(title="sre-alert-lab-demo-service")
LOGGER = logging.getLogger("sre-alert-lab")

REQUESTS = Counter(
    "demo_http_requests_total",
    "HTTP requests handled by the demo alert lab service.",
    ["path", "method", "status"],
)
FAILURE_MODE = Gauge(
    "demo_failure_mode",
    "Whether the demo service is currently forced into 503 mode.",
)
INCIDENT_SIGNAL = Gauge(
    "demo_incident_signal",
    "Synthetic alert signal for the incident lab scenarios.",
    ["scenario", "incident_id"],
)
MODE_INFO = Gauge(
    "demo_mode_info",
    "Mode indicator for the demo alert lab service.",
    ["mode"],
)

SCENARIO_SIGNALS: Dict[str, Dict[str, str]] = {
    "INC-001": {"mode": "crashloop", "scenario": "crashloop"},
    "INC-002": {"mode": "error503", "scenario": "service503"},
    "INC-003": {"mode": "dnsfailure", "scenario": "dnsfailure"},
    "INC-007": {"mode": "deployment_regression", "scenario": "deployment_regression"},
    "INC-AMB": {"mode": "ambiguous", "scenario": "ambiguous"},
}

KNOWN_MODES = ("ok", "dependency503", "ambiguous") + tuple(item["mode"] for item in SCENARIO_SIGNALS.values())
Mode = Literal[
    "ok",
    "error503",
    "crashloop",
    "dnsfailure",
    "dependency503",
    "deployment_regression",
    "ambiguous",
]
STATE: dict[str, Mode] = {"mode": "ok"}


def _record(path: str, method: str, status: int) -> None:
    REQUESTS.labels(path=path, method=method, status=str(status)).inc()


def _signal_active(incident_id: str, mode: str) -> bool:
    payload = SCENARIO_SIGNALS[incident_id]
    return mode == payload["mode"] or (incident_id == "INC-002" and mode == "dependency503")


def _set_mode(mode: Mode) -> dict[str, str]:
    STATE["mode"] = mode
    FAILURE_MODE.set(1 if mode in {"error503", "dependency503", "deployment_regression"} else 0)
    for known_mode in KNOWN_MODES:
        MODE_INFO.labels(mode=known_mode).set(1 if mode == known_mode else 0)

    for incident_id, payload in SCENARIO_SIGNALS.items():
        active = _signal_active(incident_id, mode)
        INCIDENT_SIGNAL.labels(scenario=payload["scenario"], incident_id=incident_id).set(1 if active else 0)
    return {"mode": mode}


@app.get("/")
def root() -> dict[str, str]:
    if STATE["mode"] == "ambiguous":
        time.sleep(0.2)
        LOGGER.warning("intermittent request latency observed; no single dependency or resource cause is confirmed")
        _record("/", "GET", 200)
        return {"ok": "true", "mode": STATE["mode"]}
    if STATE["mode"] == "dependency503":
        payments_url = os.getenv("PAYMENTS_URL", "http://payments:8088/status")
        try:
            urllib.request.urlopen(payments_url, timeout=0.5).read()
        except Exception as exc:
            LOGGER.error("upstream payments dependency failed: %s", exc)
            _record("/", "GET", 503)
            raise HTTPException(status_code=503, detail="payments upstream unavailable") from exc
    if STATE["mode"] == "dnsfailure":
        hostname = os.getenv("BROKEN_DEPENDENCY_HOST", "payments.invalid.sre-lab")
        try:
            socket.getaddrinfo(hostname, 443)
        except OSError as exc:
            LOGGER.error("DNS NXDOMAIN could not resolve host %s: %s", hostname, exc)
            _record("/", "GET", 503)
            raise HTTPException(status_code=503, detail=f"could not resolve host {hostname}") from exc
    if STATE["mode"] in {"error503", "deployment_regression"}:
        if STATE["mode"] == "deployment_regression":
            LOGGER.error("new release regression: checkout handler returns 503")
        _record("/", "GET", 503)
        raise HTTPException(status_code=503, detail="demo upstream timeout")
    _record("/", "GET", 200)
    return {"ok": "true", "mode": STATE["mode"]}


@app.get("/status")
def status() -> dict[str, Any]:
    _record("/status", "GET", 200)
    mode = STATE["mode"]
    return {
        "mode": mode,
        "signals": {
            incident_id: _signal_active(incident_id, mode)
            for incident_id in SCENARIO_SIGNALS
        },
    }


@app.post("/admin/mode/{mode}")
def set_mode(mode: Mode) -> dict[str, str]:
    _record(f"/admin/mode/{mode}", "POST", 200)
    return _set_mode(mode)


@app.post("/admin/reset")
def reset_mode() -> dict[str, str]:
    _record("/admin/reset", "POST", 200)
    return _set_mode("ok")


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


_set_mode("ok")
