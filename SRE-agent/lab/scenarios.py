from __future__ import annotations

from typing import Any, Dict


def _signal_query(scenario: str, incident_id: str) -> str:
    return f'demo_incident_signal{{scenario="{scenario}",incident_id="{incident_id}"}}'


ALERT_LAB_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "INC-001": {
        "mode": "crashloop",
        "title": "CrashLoop / OOM",
        "alertname": "Demo Checkout CrashLoop OOM Signal",
        "query": _signal_query("crashloop", "INC-001"),
        "resolved_below": 0.5,
        "assistant_hint": "Pods are restarting and memory-related evidence should dominate.",
    },
    "INC-002": {
        "mode": "error503",
        "title": "High 5xx / 503",
        "alertname": "Demo API High 5xx Rate",
        "query": (
            'sum(rate(demo_http_requests_total{path="/",status=~"5.."}[1m])) '
            '/ clamp_min(sum(rate(demo_http_requests_total{path="/"}[1m])), 0.001)'
        ),
        "resolved_below": 0.2,
        "assistant_hint": "The assistant should treat this as a live service incident with dependency-style evidence.",
    },
    "INC-003": {
        "mode": "dnsfailure",
        "title": "DNS Failure",
        "alertname": "Demo Worker DNS Failure Signal",
        "query": _signal_query("dnsfailure", "INC-003"),
        "resolved_below": 0.5,
        "assistant_hint": "Logs and cluster DNS context should dominate the diagnosis.",
    },
    "KIND-OOM": {
        "mode": "oom",
        "title": "Kind OOMKilled worker",
        "alertname": "Kind Worker OOMKilled",
        "query": 'max(kube_pod_container_status_last_terminated_reason{namespace="sre-lab",pod=~"oom-worker-.*",reason="OOMKilled"})',
        "resolved_below": 0.5,
        "assistant_hint": "The terminated container state and restart loop must support the diagnosis; no automatic repair is expected.",
    },
    "KIND-DEP-503": {
        "mode": "dependency503",
        "title": "Kind dependency-driven 503",
        "alertname": "Kind Demo API Dependency 503",
        "query": (
            '(max(demo_mode_info{mode="dependency503"}) == 1) * '
            '(sum(rate(demo_http_requests_total{path="/",status=~"5.."}[1m])) '
            '/ clamp_min(sum(rate(demo_http_requests_total{path="/"}[1m])), 0.001))'
        ),
        "resolved_below": 0.2,
        "assistant_hint": "Logs and dependency workload state should identify the unavailable payments deployment.",
    },
    "KIND-DEPLOY-REGRESSION": {
        "mode": "deployment_regression",
        "title": "Kind deployment regression",
        "alertname": "Kind Demo API Deployment Regression",
        "query": _signal_query("deployment_regression", "INC-007"),
        "resolved_below": 0.5,
        "assistant_hint": "The bad-release annotation and correlated error logs must support a deployment regression.",
    },
    "KIND-DNS": {
        "mode": "dnsfailure",
        "title": "Kind workload DNS failure",
        "alertname": "Kind Demo API DNS Failure",
        "query": _signal_query("dnsfailure", "INC-003"),
        "resolved_below": 0.5,
        "assistant_hint": "Workload logs must show a concrete resolver error; broad cluster DNS repair should not be guessed.",
    },
    "KIND-AMBIGUOUS": {
        "mode": "ambiguous",
        "title": "Kind ambiguous latency",
        "alertname": "Kind Demo API Ambiguous Latency",
        "query": _signal_query("ambiguous", "INC-AMB"),
        "resolved_below": 0.5,
        "assistant_hint": "The expected outcome is escalation without an executable remediation.",
    },
}


def get_alert_lab_scenario(incident_id: str) -> Dict[str, Any] | None:
    return ALERT_LAB_SCENARIOS.get(str(incident_id).strip().upper())


def is_alert_lab_incident(incident_id: str) -> bool:
    return get_alert_lab_scenario(incident_id) is not None
