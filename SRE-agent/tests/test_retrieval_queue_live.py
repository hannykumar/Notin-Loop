from __future__ import annotations

import json

from agent.evidence_graph import build_evidence_graph
from agent.retrieval import retrieve_runbook_context
from evaluation.live_backends import run_live_backend_probe
from runtime.job_queue import RedisDurableJobQueue, reset_job_queue


def test_retrieval_ignores_inactive_tls_runbook_for_small_path() -> None:
    incident = {
        "service": "ingress",
        "severity": "sev1",
        "namespace": "prod",
        "description": "Clients are seeing x509 certificate expired and tls handshake failed on the ingress path.",
    }

    result = retrieve_runbook_context(incident["description"], incident=incident, evidence={}, limit=5)

    assert result["predicted_type"] == "Unknown"
    assert all(item["incident_type"] != "TLSCertificateExpiry" for item in result["retrieved"])
    assert result["retrieval_quality"]["needs_retry"] is True


def test_retrieval_prefers_dns_context_for_dns_alert() -> None:
    incident = {
        "service": "checkout",
        "severity": "sev2",
        "namespace": "prod",
        "description": "Checkout pods are failing with NXDOMAIN and could not resolve host errors.",
    }

    result = retrieve_runbook_context(incident["description"], incident=incident, evidence={}, limit=5)

    assert result["predicted_type"] == "DNSFailure"
    assert any(item["incident_type"] == "DNSFailure" for item in result["retrieved"][:2])
    assert result["retrieval_explanation"]["selection_strategy"] == "lexical + tfidf + evidence context + diversity"


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    def lpush(self, key: str, value: str):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def brpop(self, key: str, timeout: int = 5):
        values = self.lists.get(key, [])
        if values:
            return key, values.pop()
        raise KeyboardInterrupt("stop worker")

    def delete(self, key: str):
        self.kv.pop(key, None)
        return 1


def test_redis_durable_queue_submits_and_drains(monkeypatch) -> None:
    fake_redis = _FakeRedis()
    seen: list[dict[str, object]] = []

    monkeypatch.setattr("runtime.job_queue.redis.from_url", lambda *args, **kwargs: fake_redis)
    monkeypatch.setattr("runtime.job_queue.dispatch_job", lambda job: seen.append(job))
    reset_job_queue()

    queue = RedisDurableJobQueue(
        redis_url="redis://fake:6379/0",
        queue_name="sre_agent_jobs",
        group_ttl_seconds=300,
        pop_timeout_seconds=1,
    )

    accepted = queue.submit("plan", "run_1", "plan|INC-001", {"run_id": "run_1", "incident_id": "INC-001"})
    duplicate = queue.submit("plan", "run_2", "plan|INC-001", {"run_id": "run_2", "incident_id": "INC-001"})

    assert accepted is True
    assert duplicate is False
    assert fake_redis.llen("sre_agent_jobs") == 1

    try:
        queue.worker_loop()
    except KeyboardInterrupt:
        pass

    assert seen == [{"job_type": "plan", "payload": {"run_id": "run_1", "incident_id": "INC-001"}}]
    assert fake_redis.kv == {}


class _FakeLiveBackend:
    def get_metrics(self, service: str, namespace: str):
        return {"error_rate_percent": 2.1}

    def query_prometheus(self, expression: str):
        return {"expression": expression, "value": 1.0}

    def query_loki(self, service: str, namespace: str, limit: int = 20):
        return {"lines": ["INFO healthy"]}

    def query_tempo(self, service: str, namespace: str, limit: int = 10):
        return {"spans": [{"span": "payments", "error": ""}]}

    def get_dashboard_context(self, service: str, namespace: str):
        return {"dashboard_uid": "svc", "panels": ["error_rate"]}

    def get_recent_deploys(self, service: str, namespace: str):
        return [{"version": "svc-1", "minutes_ago": 8}]

    def get_service_owner(self, service: str):
        return {"team": "core"}

    def get_incident_history(self, service: str, limit: int = 5):
        return [{"incident_id": "H-1", "type": "Service503"}]


def test_live_backend_probe_reports_tool_health(monkeypatch) -> None:
    monkeypatch.setattr("evaluation.live_backends.LiveSREBackend", lambda: _FakeLiveBackend())

    probe = run_live_backend_probe(service="api", namespace="prod")

    assert probe["healthy_tools"] == probe["total_tools"]
    assert probe["tools"]["get_metrics"]["ok"] is True
    assert probe["tools"]["query_loki"]["payload"]["lines"] == ["INFO healthy"]


def test_evidence_graph_marks_freshness_and_source_reliability() -> None:
    graph = build_evidence_graph(
        {
            "incident": {"description": "API is failing"},
            "service_memory": {"service": "api", "summary": "Historical summary"},
            "top_chunks": [{"source_file": "04_service_503_upstream_down.md", "section": "Symptoms", "text": "503s", "incident_type": "Service503"}],
            "tool_results": {"get_metrics": {"error_rate_percent": 8.0}},
            "specialist_findings": [{"specialist": "metrics", "summary": "Error rate is elevated."}],
        }
    )

    by_type = {item["source_type"]: item for item in graph}
    assert by_type["alert"]["freshness"] == "current"
    assert by_type["tool"]["source_reliability"] >= 0.9
    assert by_type["runbook"]["freshness"] == "reference"
    assert by_type["service_memory"]["freshness"] == "historical"
