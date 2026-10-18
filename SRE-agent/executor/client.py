from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict
from uuid import uuid4

from executor.schemas import ExecutionRequest, RollbackRequest
from executor.service import ExecutionService
from runtime.resilience import run_with_retry
from runtime.settings import get_settings


class ExecutorClient:
    def __init__(self, base_url: str | None = None, shared_token: str | None = None):
        settings = get_settings()
        self.base_url = base_url if base_url is not None else settings.executor_base_url
        self.shared_token = shared_token if shared_token is not None else settings.executor_shared_token
        self.timeout_seconds = settings.executor_timeout_seconds
        self.max_retries = settings.executor_max_retries

    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = ExecutionRequest(
            execution_id=str(payload.get("execution_id") or f"exec_{uuid4().hex[:12]}"),
            run_id=str(payload.get("run_id", "")),
            incident_id=str(payload["incident_id"]),
            incident=payload["incident"],
            action=payload["action"],
            execution_mode=str(payload["execution_mode"]),
            tool_mode=str(payload["tool_mode"]),
            diagnosis=str(payload.get("diagnosis", "Unknown")),
            evidence_before=payload.get("evidence_before", {}),
            rollback_commands=payload.get("rollback_commands", []),
        )

        if not self.base_url:
            response = ExecutionService().execute(request)
            response["executor_transport"] = "embedded"
            return response

        return run_with_retry(
            lambda: self._post(request),
            retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            operation_name="executor_request",
        )

    def rollback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = RollbackRequest(
            execution_id=str(payload.get("execution_id") or f"rollback_{uuid4().hex[:12]}"),
            run_id=str(payload.get("run_id", "")),
            incident_id=str(payload["incident_id"]),
            incident=payload["incident"],
            action=payload["action"],
            execution_mode=str(payload["execution_mode"]),
            tool_mode=str(payload["tool_mode"]),
        )

        if not self.base_url:
            response = ExecutionService().rollback(
                ExecutionRequest(
                    execution_id=request.execution_id,
                    run_id=request.run_id,
                    incident_id=request.incident_id,
                    incident=request.incident,
                    action=request.action,
                    execution_mode=request.execution_mode,
                    tool_mode=request.tool_mode,
                    diagnosis="Unknown",
                )
            )
            response["executor_transport"] = "embedded"
            return response

        return run_with_retry(
            lambda: self._post(request, path="/rollback"),
            retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            operation_name="executor_rollback_request",
        )

    def _post(self, request: ExecutionRequest | RollbackRequest, path: str = "/execute") -> Dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        if self.shared_token:
            headers["X-Executor-Token"] = self.shared_token
        req = urllib.request.Request(url, data=request.model_dump_json().encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
