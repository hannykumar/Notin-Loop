"""OpenAI SDK client pointed at Nebius (PRD A1)."""
from __future__ import annotations

import os
import re
import time
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI

from factory import sre_base  # noqa: F401  (loads factory_flow/.env into os.environ)

TEMPERATURE = 0.1
_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """No LLM: offline, not configured, or the API call failed. Callers fall back or go 'unsure'."""


# Adapted from SRE-agent/agent/model_runtime.py @ b4f7e0c (record_model_success/failure pattern;
# not imported because it pulls runtime.settings; cooldown and probing skipped).
_STATE_LOCK = Lock()
_STATE: dict[str, Any] = {"last_success_at": 0.0, "last_failure_at": 0.0, "last_error": ""}


def _config() -> tuple[str, str, str]:
    return os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_API_KEY", ""), os.environ.get("LLM_MODEL", "")


def _record_success() -> None:
    with _STATE_LOCK:
        _STATE.update(last_success_at=time.time(), last_error="")


def _record_failure(error: str) -> None:
    with _STATE_LOCK:
        _STATE.update(last_failure_at=time.time(), last_error=error)


def status() -> dict:
    """Which backend ran and how it last went; stages put this in their evidence."""
    base_url, _, model = _config()
    host = urlparse(base_url).hostname or ""
    with _STATE_LOCK:
        state = dict(_STATE)
    return {"provider": "nebius" if "nebius" in host else host, "model": model, **state}


def strip_fences(text: str) -> str:
    """Return the first fenced block's body if there is one, else the text; trims blank lines only."""
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip("\n")


def complete(system: str, user: str) -> str:
    """One chat completion at low temperature, fences stripped. Raises LLMUnavailable."""
    if os.environ.get("LLM_OFFLINE") == "1":
        raise LLMUnavailable("LLM_OFFLINE=1")
    base_url, api_key, model = _config()
    if not (base_url and api_key and model):
        raise LLMUnavailable("LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must be set")
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=60, max_retries=1)
        response = client.chat.completions.create(
            model=model,
            temperature=TEMPERATURE,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        text = response.choices[0].message.content or ""
    except Exception as exc:  # any SDK/network error means no LLM for this call
        error = f"{type(exc).__name__}: {exc}"
        _record_failure(error)
        raise LLMUnavailable(error) from exc
    _record_success()
    return strip_fences(text)
