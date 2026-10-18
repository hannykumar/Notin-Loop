"""integrations/kubectl_client.py

Small HTTP client for the kubectl MCP server.

The goal is to look like MockMCP so the agent can switch between:
- mock mode (local JSON + simulated cluster)
- real mode (calls a kubectl MCP server over HTTP)

Environment variables:
- MCP_SERVER_URL: base URL of the server (default http://127.0.0.1:8088)
- MCP_APPROVAL_TOKEN: optional token sent for write actions
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional

from integrations.actions import normalize_action, render_action_to_command


def _join_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = "/" + path.lstrip("/")
    return base + path


class KubectlMCPClient:
    def __init__(self, base_url: Optional[str] = None, approval_token: Optional[str] = None):
        self.base_url = base_url or os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8088")
        self.approval_token = approval_token if approval_token is not None else os.getenv("MCP_APPROVAL_TOKEN", "")

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = _join_url(self.base_url, path)
        data = json.dumps(payload).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        # Token is only required for writes if the server demands it.
        # Sending it on reads is harmless.
        if self.approval_token:
            headers["X-Approval-Token"] = self.approval_token

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    # -----------------
    # Read tools
    # -----------------
    def get_pod_status(self, service: str, namespace: str) -> Dict[str, Any]:
        return self._post("/get_pod_status", {"service": service, "namespace": namespace})

    def describe_pod(self, pod_name: str, namespace: str) -> Dict[str, Any]:
        return self._post("/describe_pod", {"pod_name": pod_name, "namespace": namespace})

    def get_pod_logs(self, pod_name: str, namespace: str, lines: int = 200, since_time: str = "") -> List[str]:
        out = self._post(
            "/get_pod_logs",
            {"pod_name": pod_name, "namespace": namespace, "lines": lines, "since_time": since_time},
        )
        return out.get("lines", [])

    def get_cluster_events(self, namespace: str) -> List[str]:
        out = self._post("/get_events", {"namespace": namespace})
        raw = out.get("raw", "")
        return raw.splitlines() if raw else []

    def top_pod(self, pod_name: str, namespace: str) -> Dict[str, Any]:
        return self._post("/top_pod", {"pod_name": pod_name, "namespace": namespace})

    # -----------------
    # Write tools
    # -----------------
    def restart_pod(self, service: str, namespace: str) -> Dict[str, Any]:
        # Server restarts pods for app=<service>
        return self._post("/restart_pod", {"service": service, "namespace": namespace})

    def scale_deployment(self, deployment: str, namespace: str, replicas: int) -> Dict[str, Any]:
        return self._post("/scale_deployment", {"deployment": deployment, "namespace": namespace, "replicas": replicas})

    def restart_coredns(self, namespace: str) -> Dict[str, Any]:
        # Usually namespace should be "kube-system"
        return self._post("/restart_coredns", {"namespace": namespace})

    def execute_remediation(self, action: Dict[str, Any], incident: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_action(action, incident)
        command = render_action_to_command(normalized, incident)
        action_type = normalized["action_type"]

        if action_type == "restart_pod":
            result = self.restart_pod(service=normalized["target"], namespace=normalized["namespace"])
        elif action_type in {"scale_deployment", "gitops_scale_deployment"}:
            result = self.scale_deployment(
                deployment=normalized["target"],
                namespace=normalized["namespace"],
                replicas=int(normalized["replicas"]),
            )
        else:
            result = self.restart_coredns(namespace=normalized["namespace"])

        return {
            "status": "ok",
            "tool": "execute_remediation",
            "command": command,
            "action": normalized,
            "result": result,
        }
