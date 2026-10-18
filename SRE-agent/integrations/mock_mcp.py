from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from integrations.actions import normalize_action, render_action_to_command
from integrations.incident_registry import validate_incident_id
from integrations.service_context import dashboards, incident_history, owner, recent_deploys, trace_summary
from integrations.service_context import service_context
from integrations.query_safety import validate_limit, validate_query_text, validate_time_range

MOCK_INCIDENTS_DIR = Path("lab/fixtures/incidents")
LIVE_STATE_DIR = Path(os.getenv("SRE_MOCK_STATE_DIR", "data/mock_live_state"))


def _state_file(incident_id: str) -> Path:
    return LIVE_STATE_DIR / f"{validate_incident_id(incident_id)}.json"


class MockMCP:
    """
    Mock MCP surface for the agent.

    Read tools return stable fake cluster data.
    Write actions are routed through execute_remediation().
    """

    # Class-level store so multiple instances see the same evolving incident state
    _LIVE_STATE: Dict[str, Dict[str, Any]] = {}

    def __init__(self, incident_id: str, trace: Optional[object] = None, allow_write: bool = False):
        _state_file(incident_id)
        self.incident_id = incident_id
        self.trace = trace
        self.allow_write = allow_write

        # Initialize live state once (deep copy so we can safely mutate)
        if incident_id not in self._LIVE_STATE:
            self._LIVE_STATE[incident_id] = copy.deepcopy(self._load_live_state(incident_id))

        # Always read/write from live state
        self.data = self._LIVE_STATE[incident_id]

    # -----------------------
    # Step 10: reset helpers
    # -----------------------
    @classmethod
    def reset_live_state(cls, incident_id: Optional[str] = None) -> None:
        """
        Reset the in-memory "cluster state".

        - If incident_id is None: clears ALL incident live states.
        - If incident_id is set: clears only that incident's live state.
        """
        if incident_id is None:
            cls._LIVE_STATE.clear()
            if LIVE_STATE_DIR.exists():
                for fp in LIVE_STATE_DIR.glob("*.json"):
                    fp.unlink()
        else:
            state_path = _state_file(incident_id)
            cls._LIVE_STATE.pop(incident_id, None)
            if state_path.exists():
                state_path.unlink()

    @classmethod
    def seed_live_state(cls, incident_id: str, payload: Dict[str, Any]) -> None:
        """
        Seed both in-memory and file-backed live state so local and MCP subprocess
        clients observe the same incident payload.
        """
        state_path = _state_file(incident_id)
        LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        seeded = copy.deepcopy(payload)
        cls._LIVE_STATE[incident_id] = seeded
        state_path.write_text(json.dumps(seeded, indent=2), encoding="utf-8")

    # -----------------------
    # Load incident JSON
    # -----------------------
    def _load_incident(self, incident_id: str) -> Dict[str, Any]:
        for fp in MOCK_INCIDENTS_DIR.glob("*.json"):
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("incident_id") == incident_id:
                return d
        raise FileNotFoundError(f"Incident {incident_id} not found in {MOCK_INCIDENTS_DIR}")

    def _state_path(self, incident_id: str) -> Path:
        path = _state_file(incident_id)
        LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        return path

    def _load_live_state(self, incident_id: str) -> Dict[str, Any]:
        state_path = self._state_path(incident_id)
        if state_path.exists():
            return json.loads(state_path.read_text(encoding="utf-8"))
        data = self._load_incident(incident_id)
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def _save_live_state(self) -> None:
        self._state_path(self.incident_id).write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    # -----------------------
    # Read tools
    # -----------------------
    def get_pod_status(self, service: str, namespace: str) -> Dict[str, Any]:
        return self.data["pod_status"]

    def describe_pod(self, pod_name: str, namespace: str) -> Dict[str, Any]:
        return self.data["pod_describe"]

    def get_metrics(self, service: str, namespace: str) -> Dict[str, Any]:
        return self.data["metrics"]

    def get_pod_logs(self, pod_name: str, namespace: str, lines: int = 200, since_time: str = "") -> List[str]:
        # Fixture lines have no timestamps; preserve the live tool's argument contract.
        logs = self.data["logs_tail"]
        return logs[-min(lines, len(logs)) :]

    def get_cluster_events(self, namespace: str) -> List[str]:
        return self.data["cluster_events"]

    def get_kubernetes_events(self, namespace: str) -> List[str]:
        return self.get_cluster_events(namespace=namespace)

    def get_deployment(self, service: str, namespace: str) -> Dict[str, Any]:
        return {
            "name": service,
            "namespace": namespace,
            "replicas": self.data.get("pod_status", {}).get("replicas", 0),
            "current_version": self.data.get("current_version", ""),
            "previous_version": self.data.get("previous_version", ""),
        }

    def query_prometheus(self, expression: str) -> Dict[str, Any]:
        metrics = self.data.get("metrics", {})
        metric_name = "synthetic_score"
        value: float | int | None = None
        lowered = str(expression).lower()
        if "error" in lowered:
            metric_name = "error_rate_percent"
            value = metrics.get("error_rate_percent", metrics.get("error_rate"))
        elif "dns" in lowered:
            metric_name = "dns_error_rate_percent"
            value = metrics.get("dns_error_rate_percent", metrics.get("dns_error_rate"))
        elif "memory" in lowered:
            metric_name = "memory_percent"
            value = metrics.get("memory_percent")
        elif "latency" in lowered or "p95" in lowered:
            metric_name = "p95_latency_ms"
            value = metrics.get("p95_latency_ms")
        elif "cpu" in lowered:
            metric_name = "cpu_percent"
            value = metrics.get("cpu_percent")
        return {"expression": expression, "metric_name": metric_name, "value": value}

    def query_prometheus_range(self, expression: str, start: str, end: str, step_seconds: int = 30) -> Dict[str, Any]:
        expression = validate_query_text(expression)
        start_at, end_at = validate_time_range(start, end)
        instant = self.query_prometheus(expression)
        return {**instant, "start": start_at.isoformat(), "end": end_at.isoformat(), "step_seconds": max(int(step_seconds), 15), "values": [instant.get("value")]}

    def query_loki(self, service: str, namespace: str, limit: int = 50) -> Dict[str, Any]:
        lines = self.get_pod_logs(self.data["pod_status"]["pod_name"], namespace=namespace, lines=limit)
        return {"query": f'{{service=\"{service}\",namespace=\"{namespace}\"}}', "lines": lines[-limit:]}

    def query_loki_range(self, query: str, start: str, end: str, limit: int = 50) -> Dict[str, Any]:
        query = validate_query_text(query)
        start_at, end_at = validate_time_range(start, end)
        limit = validate_limit(limit)
        lines = list(self.data.get("logs_tail", []))[-limit:]
        return {"query": query, "start": start_at.isoformat(), "end": end_at.isoformat(), "lines": lines}

    def query_tempo(self, service: str, namespace: str, limit: int = 20) -> Dict[str, Any]:
        spans = trace_summary(service)[:limit]
        return {"service": service, "namespace": namespace, "spans": spans}

    def get_dashboard_context(self, service: str, namespace: str) -> Dict[str, Any]:
        panels = dashboards(service)
        dashboard = panels[0] if panels else {}
        return {
            "service": service,
            "namespace": namespace,
            "dashboard_uid": dashboard.get("uid", ""),
            "dashboard_title": dashboard.get("title", ""),
            "panels": dashboard.get("panels", []),
        }

    def get_recent_deploys(self, service: str, namespace: str) -> List[Dict[str, Any]]:
        return list(self.data.get("recent_deploys") or recent_deploys(service))

    def get_service_owner(self, service: str) -> Dict[str, Any]:
        return owner(service)

    def get_incident_history(self, service: str, limit: int = 10) -> List[Dict[str, Any]]:
        return incident_history(service)[:limit]

    def get_git_diff(self, service: str, namespace: str) -> Dict[str, Any]:
        return {"service": service, "namespace": namespace, "recent_deploys": self.get_recent_deploys(service, namespace), "diff_summary": self.data.get("git_diff", "No stored diff is available.")}

    def get_service_dependencies(self, service: str, namespace: str = "") -> Dict[str, Any]:
        observed = self.data.get("service_dependencies")
        if isinstance(observed, dict):
            return dict(observed)
        return {
            "service": service,
            "dependencies": service_context(service).get("dependencies", []),
            "unavailable": [],
            "unavailable_count": 0,
        }

    def search_runbooks(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        from agent.retrieval import retrieve_runbook_chunks

        return retrieve_runbook_chunks(validate_query_text(query), limit=min(validate_limit(limit), 10))

    def search_previous_incidents(self, service: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.get_incident_history(service=service, limit=min(validate_limit(limit), 20))

    def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        tool = getattr(self, tool_name, None)
        if tool is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        return tool(**kwargs)

    # -----------------------
    # Write tools (mocked)
    # -----------------------
    def _require_write_allowed(self) -> None:
        if not self.allow_write:
            raise PermissionError("Write actions are disabled. Set allow_write=True only after human approval.")

    def _scale_metric(self, keys: List[str], factor: float, floor: float = 0.0) -> None:
        metrics = self.data.get("metrics", {})
        for key in keys:
            if key in metrics:
                metrics[key] = max(floor, float(metrics[key]) * factor)

    def _recovery_profile(self) -> str:
        return str(self.data.get("recovery_profile", "full")).strip().lower() or "full"

    def _scaled_factor(self, full: float, partial: float) -> float:
        return partial if self._recovery_profile() == "partial" else full

    def restart_pod(self, service: str, namespace: str) -> Dict[str, Any]:
        """
        Mock restart: simulate improvement by reducing error rate and clearing recent error logs.
        """
        self._require_write_allowed()

        # Increase restart count
        self.data["pod_status"]["restarts"] = int(self.data["pod_status"].get("restarts", 0)) + 1

        # Simulate: errors reduce after restart
        self._scale_metric(["error_rate", "error_rate_percent"], factor=self._scaled_factor(0.3, 0.65), floor=0.0)
        self._scale_metric(["dns_error_rate", "dns_error_rate_percent"], factor=self._scaled_factor(0.5, 0.75), floor=0.0)
        self._scale_metric(["p95_latency_ms"], factor=self._scaled_factor(0.6, 0.82), floor=50.0)
        self._scale_metric(["memory_percent"], factor=self._scaled_factor(0.85, 0.93), floor=1.0)
        self._scale_metric(["memory_rss_mb"], factor=self._scaled_factor(0.9, 0.96), floor=1.0)

        # Simulate: logs become healthier
        logs = self.data.get("logs_tail", [])
        filtered = [
            l
            for l in logs
            if "503" not in l
            and "NXDOMAIN" not in l
            and "could not resolve" not in l.lower()
            and "out of memory" not in l.lower()
            and "oom" not in l.lower()
        ]
        if self._recovery_profile() == "partial":
            filtered.append("WARN: pod restarted successfully, but elevated error signals remain")
        else:
            filtered.append("INFO: pod restarted successfully; health checks passing")
        self.data["logs_tail"] = filtered[-500:]
        self._save_live_state()

        return {"status": "ok", "action": "restart_pod", "service": service, "namespace": namespace}

    def scale_deployment(self, service: str, namespace: str, replicas: int) -> Dict[str, Any]:
        """
        Mock scale: simulate improvement by reducing saturation-like signals.
        """
        self._require_write_allowed()

        self.data["pod_status"]["replicas"] = replicas

        self._scale_metric(["cpu_percent"], factor=self._scaled_factor(0.7, 0.86), floor=5.0)
        self._scale_metric(["error_rate", "error_rate_percent"], factor=self._scaled_factor(0.5, 0.72), floor=0.0)
        self._scale_metric(["p95_latency_ms"], factor=self._scaled_factor(0.8, 0.9), floor=50.0)
        self._scale_metric(["memory_percent"], factor=self._scaled_factor(0.9, 0.95), floor=1.0)

        logs = self.data.get("logs_tail", [])
        if self._recovery_profile() == "partial":
            logs.append(f"WARN: scaled deployment to replicas={replicas}, but backlog remains elevated")
        else:
            logs.append(f"INFO: scaled deployment to replicas={replicas}")
        self.data["logs_tail"] = logs[-500:]
        self._save_live_state()

        return {
            "status": "ok",
            "action": "scale_deployment",
            "service": service,
            "namespace": namespace,
            "replicas": replicas,
        }

    def restart_coredns(self, namespace: str = "kube-system") -> Dict[str, Any]:
        """
        Mock CoreDNS restart: simulate DNS recovery.
        """
        self._require_write_allowed()

        self._scale_metric(["dns_error_rate", "dns_error_rate_percent"], factor=self._scaled_factor(0.1, 0.55), floor=0.0)
        self._scale_metric(["error_rate", "error_rate_percent"], factor=self._scaled_factor(0.3, 0.7), floor=0.0)

        logs = self.data.get("logs_tail", [])
        filtered = [l for l in logs if "nxdomain" not in l.lower() and "could not resolve" not in l.lower()]
        if self._recovery_profile() == "partial":
            filtered.append("WARN: CoreDNS restarted; DNS errors reduced but not fully cleared")
        else:
            filtered.append("INFO: CoreDNS restarted; DNS resolution normal")
        self.data["logs_tail"] = filtered[-500:]
        self._save_live_state()

        return {"status": "ok", "action": "restart_coredns", "namespace": namespace}

    def execute_remediation(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a structured remediation action after approval.
        """
        self._require_write_allowed()

        normalized = normalize_action(action, self.data)
        command = render_action_to_command(normalized, self.data)
        action_type = normalized["action_type"]

        if action_type == "restart_pod":
            result = self.restart_pod(service=normalized["target"], namespace=normalized["namespace"])
        elif action_type in {"scale_deployment", "gitops_scale_deployment"}:
            result = self.scale_deployment(
                service=normalized["target"],
                namespace=normalized["namespace"],
                replicas=int(normalized["replicas"]),
            )
        elif action_type == "gitops_rollback_deployment":
            self._scale_metric(["error_rate", "error_rate_percent"], factor=0.15, floor=0.0)
            self._scale_metric(["p95_latency_ms"], factor=0.35, floor=50.0)
            self.data["logs_tail"] = ["INFO deployment rollback completed; previous version is healthy"]
            self.data["current_version"] = normalized["previous_version"]
            self._save_live_state()
            result = {
                "status": "ok",
                "action": action_type,
                "service": normalized["target"],
                "version": normalized["previous_version"],
            }
        else:
            result = self.restart_coredns(namespace=normalized["namespace"])

        return {
            "status": "ok",
            "tool": "execute_remediation",
            "command": command,
            "action": normalized,
            "result": result,
        }

    def noop(self, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "noop", "details": kwargs}
