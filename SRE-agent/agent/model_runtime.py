from __future__ import annotations

import json
import time
from threading import Lock
from typing import Any, Dict
from urllib.request import Request, urlopen

from runtime.settings import get_settings

_STATE_LOCK = Lock()
_MODEL_STATE: Dict[str, Any] = {
    "provider": "",
    "model": "",
    "last_success_at": 0.0,
    "last_failure_at": 0.0,
    "cooldown_until": 0.0,
    "last_error": "",
    "last_probe_at": 0.0,
    "last_probe_result": {},
}


def _provider_key() -> tuple[str, str]:
    settings = get_settings()
    return settings.planner_provider, settings.planner_model


def _base_status() -> Dict[str, Any]:
    provider, model = _provider_key()
    now = time.time()
    with _STATE_LOCK:
        if _MODEL_STATE.get("provider") != provider or _MODEL_STATE.get("model") != model:
            _MODEL_STATE.update(
                {
                    "provider": provider,
                    "model": model,
                    "last_success_at": 0.0,
                    "last_failure_at": 0.0,
                    "cooldown_until": 0.0,
                    "last_error": "",
                    "last_probe_at": 0.0,
                    "last_probe_result": {},
                }
            )
        status = dict(_MODEL_STATE)

    cooldown_remaining = max(float(status.get("cooldown_until", 0.0)) - now, 0.0)
    ready = provider == "deterministic" or bool(status.get("last_success_at")) and cooldown_remaining <= 0.0
    if provider == "deterministic":
        state = "disabled"
    elif cooldown_remaining > 0.0:
        state = "cooldown"
    elif ready:
        state = "ready"
    elif status.get("last_failure_at"):
        state = "degraded"
    else:
        state = "unknown"
    return {
        "provider": provider,
        "model": model,
        "status": state,
        "ready": ready,
        "cooldown_active": cooldown_remaining > 0.0,
        "cooldown_remaining_seconds": round(cooldown_remaining, 2),
        "last_success_at": status.get("last_success_at", 0.0),
        "last_failure_at": status.get("last_failure_at", 0.0),
        "last_error": status.get("last_error", ""),
        "last_probe_at": status.get("last_probe_at", 0.0),
        "last_probe_result": status.get("last_probe_result", {}),
    }


def record_model_success() -> None:
    provider, model = _provider_key()
    now = time.time()
    with _STATE_LOCK:
        _MODEL_STATE.update(
            {
                "provider": provider,
                "model": model,
                "last_success_at": now,
                "last_error": "",
                "cooldown_until": 0.0,
            }
        )


def record_model_failure(error: str) -> None:
    settings = get_settings()
    provider, model = _provider_key()
    now = time.time()
    with _STATE_LOCK:
        _MODEL_STATE.update(
            {
                "provider": provider,
                "model": model,
                "last_failure_at": now,
                "last_error": str(error),
                "cooldown_until": now + max(float(settings.planner_failure_cooldown_seconds), 0.0),
            }
        )


def passive_model_status() -> Dict[str, Any]:
    return _base_status()


def _probe_ollama(base_url: str, timeout_seconds: float) -> Dict[str, Any]:
    req = Request(
        url=f"{base_url.rstrip('/')}/api/tags",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    started = time.perf_counter()
    with urlopen(req, timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    models = [str(item.get("name", "")) for item in list(payload.get("models") or []) if isinstance(item, dict)]
    return {"reachable": True, "latency_ms": latency_ms, "models": models}


def _probe_openai_compatible(base_url: str, api_key: str, timeout_seconds: float) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(
        url=f"{base_url.rstrip('/')}/v1/models",
        headers=headers,
        method="GET",
    )
    started = time.perf_counter()
    with urlopen(req, timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    models = [str(item.get("id", "")) for item in list(payload.get("data") or []) if isinstance(item, dict)]
    return {"reachable": True, "latency_ms": latency_ms, "models": models}


def active_model_status(force: bool = False) -> Dict[str, Any]:
    settings = get_settings()
    status = _base_status()
    if settings.planner_provider == "deterministic":
        return {
            **status,
            "detail": "Deterministic planner is active. No external model probe is required.",
        }

    if not force and status.get("last_probe_result") and (
        time.time() - float(status.get("last_probe_at", 0.0)) < max(float(settings.planner_probe_ttl_seconds), 1.0)
    ):
        return {**status, **dict(status.get("last_probe_result") or {})}

    try:
        if settings.planner_provider in {"openai", "openai_compatible"}:
            probe = _probe_openai_compatible(
                settings.planner_base_url,
                settings.planner_api_key,
                settings.planner_probe_timeout_seconds,
            )
        else:
            probe = _probe_ollama(settings.planner_base_url, settings.planner_probe_timeout_seconds)
        models = probe.get("models", [])
        configured_model = settings.planner_model
        model_available = configured_model in models or (
            settings.planner_provider not in {"openai", "openai_compatible"}
            and ":" not in configured_model
            and f"{configured_model}:latest" in models
        )
        probe.update(
            {
                "status": "ready" if model_available else "missing_model",
                "ready": model_available,
                "provider": settings.planner_provider,
                "model": settings.planner_model,
                "detail": (
                    "Configured model is installed; inference has not been tested by this probe."
                    if model_available else f"Endpoint is reachable but model '{configured_model}' is not installed."
                ),
            }
        )
        with _STATE_LOCK:
            _MODEL_STATE["last_probe_at"] = time.time()
            _MODEL_STATE["last_probe_result"] = probe
        return {**_base_status(), **probe}
    except Exception as exc:
        error_text = str(exc)
        record_model_failure(error_text)
        probe = {
            "status": "degraded",
            "ready": False,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            "detail": "Model endpoint probe failed.",
            "probe_error": error_text,
        }
        with _STATE_LOCK:
            _MODEL_STATE["last_probe_at"] = time.time()
            _MODEL_STATE["last_probe_result"] = probe
        return {**_base_status(), **probe}


def reset_model_runtime_state() -> None:
    with _STATE_LOCK:
        _MODEL_STATE.update(
            {
                "provider": "",
                "model": "",
                "last_success_at": 0.0,
                "last_failure_at": 0.0,
                "cooldown_until": 0.0,
                "last_error": "",
                "last_probe_at": 0.0,
                "last_probe_result": {},
            }
        )
