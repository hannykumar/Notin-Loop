from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

from runtime.settings import get_settings
from integrations.service_context import dashboards, incident_history, owner, recent_deploys, service_context
from integrations.query_safety import validate_limit, validate_query_text, validate_time_range, validate_kubernetes_name


def _safe_json_load(raw: str) -> Dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("Expected a JSON object from backend")
    return payload


class KubectlLiveAdapter:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _base_cmd(self) -> List[str]:
        cmd = [self.settings.kubectl_binary]
        if self.settings.kubeconfig_path:
            cmd.extend(["--kubeconfig", self.settings.kubeconfig_path])
        if self.settings.kubectl_context:
            cmd.extend(["--context", self.settings.kubectl_context])
        return cmd

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            [*self._base_cmd(), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.settings.tool_timeout_seconds,
        )
        return completed.stdout

    def get_pod_status(self, service: str, namespace: str) -> Dict[str, Any]:
        validate_kubernetes_name(service)
        validate_kubernetes_name(namespace, namespace=True)
        raw = self._run("get", "pods", "-n", namespace, "-l", f"app={service}", "-o", "json")
        data = _safe_json_load(raw)
        items = data.get("items", [])
        if not items:
            raise RuntimeError(f"No pods found for app={service} in namespace={namespace}")

        pod = max(items, key=lambda item: sum(
            int(status.get("restartCount", 0) or 0) + (1000 if status.get("state", {}).get("waiting") else 0)
            for status in item.get("status", {}).get("containerStatuses", [])
        ))
        statuses = pod.get("status", {}).get("containerStatuses", [])
        state = statuses[0].get("state", {}) if statuses else {}
        waiting_reason = state.get("waiting", {}).get("reason") if isinstance(state, dict) else None
        return {
            "pod_name": pod.get("metadata", {}).get("name", ""),
            "phase": pod.get("status", {}).get("phase", "Unknown"),
            "restarts": sum(int(status.get("restartCount", 0) or 0) for status in statuses),
            "reason": waiting_reason or pod.get("status", {}).get("reason", "Running"),
            "replicas": len(items),
        }

    def describe_pod(self, pod_name: str, namespace: str) -> Dict[str, Any]:
        validate_kubernetes_name(pod_name)
        validate_kubernetes_name(namespace, namespace=True)
        raw = self._run("get", "pod", pod_name, "-n", namespace, "-o", "json")
        pod = _safe_json_load(raw)
        statuses = pod.get("status", {}).get("containerStatuses", [])
        last_state = statuses[0].get("lastState", {}) if statuses else {}
        resources = pod.get("spec", {}).get("containers", [{}])[0].get("resources", {})
        return {
            "last_state": last_state,
            "limits": resources.get("limits", {}),
            "requests": resources.get("requests", {}),
        }

    def get_pod_logs(self, pod_name: str, namespace: str, lines: int = 200, since_time: str = "") -> List[str]:
        validate_kubernetes_name(pod_name)
        validate_kubernetes_name(namespace, namespace=True)
        lines = validate_limit(lines)
        args = ["logs", pod_name, "-n", namespace, f"--tail={int(lines)}"]
        if str(since_time or "").strip():
            args.append(f"--since-time={str(since_time).strip()}")
        raw = self._run(*args)
        return [line for line in raw.splitlines() if line.strip()]

    def get_cluster_events(self, namespace: str) -> List[str]:
        validate_kubernetes_name(namespace, namespace=True)
        raw = self._run("get", "events", "-n", namespace, "--sort-by=.lastTimestamp", "-o", "json")
        data = _safe_json_load(raw)
        return [
            f"{item.get('type', 'Normal')} {item.get('reason', '')} {item.get('message', '')}".strip()
            for item in data.get("items", [])[-20:]
        ]

    def get_deployment(self, service: str, namespace: str) -> Dict[str, Any]:
        validate_kubernetes_name(service)
        validate_kubernetes_name(namespace, namespace=True)
        raw = self._run("get", "deployment", service, "-n", namespace, "-o", "json")
        deployment = _safe_json_load(raw)
        containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        return {
            "name": deployment.get("metadata", {}).get("name", service),
            "namespace": namespace,
            "generation": deployment.get("metadata", {}).get("generation"),
            "replicas": deployment.get("spec", {}).get("replicas"),
            "ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
            "images": [item.get("image", "") for item in containers],
            "annotations": deployment.get("metadata", {}).get("annotations", {}),
        }


class PrometheusAdapter:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.prometheus_base_url:
            raise RuntimeError("PROMETHEUS_BASE_URL is required for the live metrics backend")

    def _query(self, expression: str) -> float | None:
        encoded = urllib.parse.urlencode({"query": expression})
        url = f"{self.settings.prometheus_base_url.rstrip('/')}/api/v1/query?{encoded}"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = _safe_json_load(response.read().decode("utf-8"))
        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        value = result[0].get("value", [None, None])[1]
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None

    def get_metrics(self, service: str, namespace: str) -> Dict[str, Any]:
        filters = f'namespace="{namespace}",service="{service}"'
        requests_metric = os.getenv("PROMETHEUS_HTTP_REQUESTS_METRIC", "http_requests_total").strip()
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", requests_metric):
            raise ValueError("PROMETHEUS_HTTP_REQUESTS_METRIC must be a valid Prometheus metric name")
        rate_window = os.getenv("PROMETHEUS_RATE_WINDOW", "5m").strip()
        if not re.fullmatch(r"[1-9][0-9]*[smh]", rate_window):
            raise ValueError("PROMETHEUS_RATE_WINDOW must be a positive Prometheus duration such as 1m or 5m")
        expressions = {
            "error_rate_percent": (
                f'(sum(rate({requests_metric}{{{filters},status=~"5.."}}[{rate_window}])) / '
                f'clamp_min(sum(rate({requests_metric}{{{filters}}}[{rate_window}])), 0.001)) * 100'
            ),
            "p95_latency_ms": (
                f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{{filters}}}[5m])) by (le)) * 1000'
            ),
            "cpu_percent": f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod=~"{service}.*"}}[5m])) * 100',
            "memory_percent": (
                f'(sum(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{service}.*"}}) / clamp_min(sum(kube_pod_container_resource_limits{{namespace="{namespace}",pod=~"{service}.*",resource="memory"}}), 1)) * 100'
            ),
            "dns_error_rate_percent": (
                '(sum(rate(coredns_dns_responses_total{rcode=~"NXDOMAIN|SERVFAIL"}[5m])) / clamp_min(sum(rate(coredns_dns_responses_total[5m])), 0.001)) * 100'
            ),
        }
        metrics: Dict[str, Any] = {}
        query_errors: Dict[str, str] = {}
        for key, expression in expressions.items():
            try:
                value = self._query(expression)
            except Exception as exc:
                query_errors[key] = f"{type(exc).__name__}: {exc}"
                continue
            if value is not None:
                metrics[key] = value
        if query_errors:
            metrics["_query_errors"] = query_errors
        return metrics

    def query_range(self, expression: str, start: str, end: str, step_seconds: int = 30) -> Dict[str, Any]:
        expression = validate_query_text(expression)
        start_at, end_at = validate_time_range(start, end)
        encoded = urllib.parse.urlencode(
            {
                "query": expression,
                "start": start_at.timestamp(),
                "end": end_at.timestamp(),
                "step": max(int(step_seconds), 15),
            }
        )
        url = f"{self.settings.prometheus_base_url.rstrip('/')}/api/v1/query_range?{encoded}"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = _safe_json_load(response.read().decode("utf-8"))
        return {"expression": expression, "start": start_at.isoformat(), "end": end_at.isoformat(), "result": payload.get("data", {}).get("result", [])}


class LokiAdapter:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.loki_base_url:
            raise RuntimeError("LOKI_BASE_URL is required when SRE_LOGS_BACKEND=loki")

    def get_pod_logs(self, pod_name: str, namespace: str, lines: int = 200, since_time: str = "") -> List[str]:
        query = f'{{namespace="{namespace}", pod="{pod_name}"}}'
        params: Dict[str, Any] = {"query": query, "limit": int(lines), "direction": "BACKWARD"}
        if str(since_time or "").strip():
            start_at, end_at = validate_time_range(since_time, datetime.now(timezone.utc).isoformat())
            params.update(
                {
                    "start": int(start_at.timestamp() * 1_000_000_000),
                    "end": int(end_at.timestamp() * 1_000_000_000),
                }
            )
        encoded = urllib.parse.urlencode(params)
        url = f"{self.settings.loki_base_url.rstrip('/')}/loki/api/v1/query_range?{encoded}"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = _safe_json_load(response.read().decode("utf-8"))
        streams = payload.get("data", {}).get("result", [])
        lines_out: List[str] = []
        for stream in streams:
            for _, line in stream.get("values", []):
                lines_out.append(str(line))
        return lines_out[-int(lines) :]

    def query_range(self, query: str, start: str, end: str, limit: int = 50) -> Dict[str, Any]:
        query = validate_query_text(query)
        start_at, end_at = validate_time_range(start, end)
        limit = validate_limit(limit)
        encoded = urllib.parse.urlencode(
            {"query": query, "start": int(start_at.timestamp() * 1_000_000_000), "end": int(end_at.timestamp() * 1_000_000_000), "limit": limit, "direction": "BACKWARD"}
        )
        url = f"{self.settings.loki_base_url.rstrip('/')}/loki/api/v1/query_range?{encoded}"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = _safe_json_load(response.read().decode("utf-8"))
        return {"query": query, "start": start_at.isoformat(), "end": end_at.isoformat(), "result": payload.get("data", {}).get("result", [])}


class LiveSREBackend:
    def __init__(
        self,
        kubectl: KubectlLiveAdapter | None = None,
        metrics: PrometheusAdapter | None = None,
        logs: LokiAdapter | None = None,
    ) -> None:
        self.settings = get_settings()
        self.kubectl = kubectl or KubectlLiveAdapter()
        self.metrics = metrics
        self.logs = logs
        if self.settings.prometheus_base_url:
            self.metrics = metrics or PrometheusAdapter()
        if self.settings.logs_backend == "loki" and self.settings.loki_base_url:
            self.logs = logs or LokiAdapter()

    def _require_metrics(self) -> PrometheusAdapter:
        if self.metrics is None:
            raise RuntimeError("PROMETHEUS_BASE_URL is required for the live metrics backend")
        return self.metrics

    def get_pod_status(self, service: str, namespace: str) -> Dict[str, Any]:
        return self.kubectl.get_pod_status(service=service, namespace=namespace)

    def describe_pod(self, pod_name: str, namespace: str) -> Dict[str, Any]:
        return self.kubectl.describe_pod(pod_name=pod_name, namespace=namespace)

    def get_metrics(self, service: str, namespace: str) -> Dict[str, Any]:
        return self._require_metrics().get_metrics(service=service, namespace=namespace)

    def get_pod_logs(self, pod_name: str, namespace: str, lines: int = 200, since_time: str = "") -> List[str]:
        if self.logs is not None:
            return self.logs.get_pod_logs(pod_name=pod_name, namespace=namespace, lines=lines, since_time=since_time)
        return self.kubectl.get_pod_logs(pod_name=pod_name, namespace=namespace, lines=lines, since_time=since_time)

    def get_cluster_events(self, namespace: str) -> List[str]:
        return self.kubectl.get_cluster_events(namespace=namespace)

    def get_kubernetes_events(self, namespace: str) -> List[str]:
        return self.get_cluster_events(namespace=namespace)

    def get_deployment(self, service: str, namespace: str) -> Dict[str, Any]:
        return self.kubectl.get_deployment(service=service, namespace=namespace)

    def query_prometheus(self, expression: str) -> Dict[str, Any]:
        metrics = self._require_metrics()
        return {
            "expression": expression,
            "value": metrics._query(expression),
        }

    def query_prometheus_range(self, expression: str, start: str, end: str, step_seconds: int = 30) -> Dict[str, Any]:
        return self._require_metrics().query_range(expression=expression, start=start, end=end, step_seconds=step_seconds)

    def query_loki(self, service: str, namespace: str, limit: int = 50) -> Dict[str, Any]:
        pod = self.get_pod_status(service=service, namespace=namespace)
        lines = self.get_pod_logs(pod_name=pod["pod_name"], namespace=namespace, lines=limit)
        return {"query": f'{{service=\"{service}\",namespace=\"{namespace}\"}}', "lines": lines[-limit:]}

    def query_loki_range(self, query: str, start: str, end: str, limit: int = 50) -> Dict[str, Any]:
        if self.logs is None:
            raise RuntimeError("LOKI_BASE_URL is required for range log queries")
        return self.logs.query_range(query=query, start=start, end=end, limit=limit)

    def query_tempo(self, service: str, namespace: str, limit: int = 20) -> Dict[str, Any]:
        tempo_base_url = os.getenv("TEMPO_BASE_URL", "").strip()
        if not tempo_base_url:
            return {"service": service, "namespace": namespace, "spans": [], "status": "not_configured", "reason": "TEMPO_BASE_URL is not configured"}
        encoded = urllib.parse.urlencode({"q": service, "limit": int(limit)})
        url = f"{tempo_base_url.rstrip('/')}/api/search?{encoded}"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = _safe_json_load(response.read().decode("utf-8"))
        spans = payload.get("traces", payload.get("spans", []))
        return {"service": service, "namespace": namespace, "spans": spans[:limit]}

    def get_dashboard_context(self, service: str, namespace: str) -> Dict[str, Any]:
        dashboard = dashboards(service)[0] if dashboards(service) else {}
        return {
            "service": service,
            "namespace": namespace,
            "dashboard_uid": dashboard.get("uid", ""),
            "dashboard_title": dashboard.get("title", ""),
            "panels": dashboard.get("panels", []),
        }

    def get_recent_deploys(self, service: str, namespace: str) -> List[Dict[str, Any]]:
        observed: List[Dict[str, Any]] = []
        try:
            deployment = self.kubectl.get_deployment(service=service, namespace=namespace)
            release = str((deployment.get("annotations") or {}).get("sre-agent/release") or "").strip()
            if release:
                observed.append(
                    {
                        "version": release,
                        "source": "kubernetes_deployment_annotation",
                        "generation": deployment.get("generation"),
                        "images": deployment.get("images", []),
                        "status": "observed",
                    }
                )
        except Exception as exc:
            observed.append({"status": "unknown", "error": f"{type(exc).__name__}: {exc}"})
        observed.extend(recent_deploys(service))
        return observed

    def get_service_owner(self, service: str) -> Dict[str, Any]:
        return owner(service)

    def get_incident_history(self, service: str, limit: int = 10) -> List[Dict[str, Any]]:
        return incident_history(service)[:limit]

    def get_git_diff(self, service: str, namespace: str) -> Dict[str, Any]:
        deployment = self.get_deployment(service=service, namespace=namespace)
        return {"service": service, "namespace": namespace, "deployment": deployment, "recent_deploys": recent_deploys(service), "diff_summary": "Deployment image and annotations captured; configure a Git provider for a full diff."}

    def get_service_dependencies(self, service: str, namespace: str = "") -> Dict[str, Any]:
        context = service_context(service)
        namespace = namespace or str(context.get("namespace") or "default")
        validate_kubernetes_name(namespace, namespace=True)
        dependencies: List[Dict[str, Any]] = []
        unavailable: List[Dict[str, Any]] = []
        for configured in context.get("dependencies", []):
            if isinstance(configured, dict):
                name = str(configured.get("name") or configured.get("service") or "").strip()
                dependency_namespace = str(configured.get("namespace") or namespace)
            else:
                name = str(configured).strip()
                dependency_namespace = namespace
            if not name:
                continue
            item: Dict[str, Any] = {"name": name, "namespace": dependency_namespace, "status": "unknown"}
            try:
                deployment = self.kubectl.get_deployment(service=name, namespace=dependency_namespace)
                ready_replicas = int(deployment.get("ready_replicas", 0) or 0)
                item.update(
                    {
                        "status": "ready" if ready_replicas > 0 else "unavailable",
                        "replicas": deployment.get("replicas"),
                        "ready_replicas": ready_replicas,
                    }
                )
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            dependencies.append(item)
            if item["status"] == "unavailable":
                unavailable.append(item)
        return {
            "service": service,
            "namespace": namespace,
            "dependencies": dependencies,
            "unavailable": unavailable,
            "unavailable_count": len(unavailable),
        }

    def search_runbooks(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        from agent.retrieval import retrieve_runbook_chunks

        return retrieve_runbook_chunks(validate_query_text(query), limit=min(validate_limit(limit), 10))

    def search_previous_incidents(self, service: str, limit: int = 10) -> List[Dict[str, Any]]:
        return incident_history(service)[: min(validate_limit(limit), 20)]

    def execute_remediation(self, action: Dict[str, Any]) -> Dict[str, Any]:
        raise PermissionError("Live MCP tools are read-only. Use the isolated executor for approved remediations.")


def _probe_http_json(url: str, timeout_seconds: float) -> Dict[str, Any]:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        payload = _safe_json_load(response.read().decode("utf-8"))
    return {
        "status": "ready",
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "detail": "Endpoint responded successfully.",
        "payload_keys": sorted(list(payload.keys()))[:6],
    }


def _probe_http_text(url: str, timeout_seconds: float) -> Dict[str, Any]:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="ignore")
    return {
        "status": "ready",
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "detail": body[:120] or "Endpoint responded successfully.",
    }


def probe_live_backend(active_probe: bool = False) -> Dict[str, Any]:
    settings = get_settings()
    components: Dict[str, Dict[str, Any]] = {
        "kubectl": {
            "configured": bool(settings.kubectl_binary),
            "status": "ready" if settings.kubeconfig_path else "not_configured",
            "detail": "Kubeconfig is available." if settings.kubeconfig_path else "KUBECONFIG is not configured.",
        },
        "prometheus": {
            "configured": bool(settings.prometheus_base_url),
            "status": "ready" if settings.prometheus_base_url else "not_configured",
            "detail": "Prometheus base URL is configured." if settings.prometheus_base_url else "PROMETHEUS_BASE_URL is not configured.",
        },
        "loki": {
            "configured": bool(settings.loki_base_url),
            "status": "ready" if settings.loki_base_url else "not_configured",
            "detail": "Loki base URL is configured." if settings.loki_base_url else "LOKI_BASE_URL is not configured.",
        },
        "tempo": {
            "configured": bool(os.getenv("TEMPO_BASE_URL", "").strip()),
            "status": "ready" if os.getenv("TEMPO_BASE_URL", "").strip() else "not_configured",
            "detail": "Tempo base URL is configured." if os.getenv("TEMPO_BASE_URL", "").strip() else "TEMPO_BASE_URL is not configured.",
        },
    }

    if not active_probe:
        degraded_components = [name for name, item in components.items() if item.get("configured") and item.get("status") == "degraded"]
        return {
            "configured": settings.mcp_backend == "live",
            "active_probe": False,
            "components": components,
            "degraded_components": degraded_components,
            "ready": all(item["status"] == "ready" for item in components.values() if item.get("configured")),
        }

    if settings.kubeconfig_path:
        try:
            adapter = KubectlLiveAdapter()
            started = time.perf_counter()
            raw = adapter._run("get", "--raw=/readyz", "--request-timeout=3s")
            components["kubectl"] = {
                "configured": True,
                "status": "ready",
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "detail": f"Kubernetes API {raw.strip() or 'ready'}",
            }
        except Exception as exc:
            components["kubectl"] = {"configured": True, "status": "degraded", "detail": str(exc)}

    if settings.prometheus_base_url:
        try:
            components["prometheus"] = {
                "configured": True,
                **_probe_http_json(f"{settings.prometheus_base_url.rstrip('/')}/api/v1/status/buildinfo", 3.0),
            }
        except Exception as exc:
            components["prometheus"] = {"configured": True, "status": "degraded", "detail": str(exc)}

    if settings.loki_base_url:
        try:
            components["loki"] = {
                "configured": True,
                **_probe_http_text(f"{settings.loki_base_url.rstrip('/')}/ready", 3.0),
            }
        except Exception as exc:
            components["loki"] = {"configured": True, "status": "degraded", "detail": str(exc)}

    tempo_base_url = os.getenv("TEMPO_BASE_URL", "").strip()
    if tempo_base_url:
        try:
            components["tempo"] = {
                "configured": True,
                **_probe_http_text(f"{tempo_base_url.rstrip('/')}/ready", 3.0),
            }
        except Exception as exc:
            components["tempo"] = {"configured": True, "status": "degraded", "detail": str(exc)}

    degraded_components = [name for name, item in components.items() if item.get("configured") and item.get("status") == "degraded"]
    return {
        "configured": settings.mcp_backend == "live",
        "active_probe": True,
        "components": components,
        "degraded_components": degraded_components,
        "ready": all(item["status"] == "ready" for item in components.values() if item.get("configured")),
    }
