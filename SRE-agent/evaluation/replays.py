from __future__ import annotations

import copy
from typing import Any, Dict, List

from integrations.mock_mcp import MockMCP


def _base_incident(incident_id: str) -> Dict[str, Any]:
    return copy.deepcopy(MockMCP(incident_id).data)


def _clone(
    base: Dict[str, Any],
    replay_id: str,
    title: str,
    description: str,
    expected: str,
    should_confirm: bool = True,
) -> Dict[str, Any]:
    payload = copy.deepcopy(base)
    payload["incident_id"] = replay_id
    payload["title"] = title
    payload["description"] = description
    payload["expected_incident_type"] = expected
    payload["should_confirm"] = should_confirm
    return payload


def generate_replayed_incidents() -> List[Dict[str, Any]]:
    replays: List[Dict[str, Any]] = []

    oom = _base_incident("INC-001")
    for idx in range(1, 5):
        replay = _clone(
            oom,
            f"REPLAY-CL-{idx:03d}",
            f"Checkout crash loop replay {idx}",
            f"Checkout pods are restarting and describe shows OOMKilled. variation={idx}",
            "CrashLoopBackOff",
        )
        replay["metrics"]["memory_percent"] = min(98, 90 + idx)
        replay["pod_status"]["restarts"] = 8 + idx
        replay["logs_tail"] = oom["logs_tail"] + [f"ERROR out of memory replay {idx}"]
        replay["severity"] = "sev1"
        replay["evaluation_tags"] = ["shared_symptom", "missing_event"] if idx == 4 else ["oom"]
        replays.append(replay)

    svc = _base_incident("INC-002")
    for idx in range(1, 5):
        replay = _clone(
            svc,
            f"REPLAY-503-{idx:03d}",
            f"API 503 replay {idx}",
            f"Users see 503 responses and upstream timeout to payments. replay={idx}",
            "Service503",
        )
        replay["service"] = "api"
        replay["metrics"]["error_rate_percent"] = 8 + idx
        replay["metrics"]["p95_latency_ms"] = 1800 + (idx * 120)
        replay["logs_tail"] = svc["logs_tail"] + [f"ERROR returning 503 due to upstream timeout replay={idx}"]
        replay["severity"] = "sev1"
        replay["evaluation_tags"] = ["unrelated_deploy", "dependency_failure"] if idx == 4 else ["dependency_failure"]
        replays.append(replay)

    deploy = _base_incident("INC-007")
    for idx in range(1, 5):
        replay = _clone(
            deploy,
            f"REPLAY-DEPLOY-{idx:03d}",
            f"API deployment regression replay {idx}",
            f"API 503 errors increased after release api-2026.03.12.{idx + 1}; pods remain healthy. replay={idx}",
            "DeploymentRegression",
        )
        replay["metrics"]["error_rate_percent"] = 7 + idx
        replay["metrics"]["p95_latency_ms"] = 1050 + (idx * 140)
        replay["current_version"] = f"api-2026.03.12.{idx + 1}"
        replay["logs_tail"] = deploy["logs_tail"] + [f"ERROR regression on new release replay={idx}"]
        replay["evaluation_tags"] = ["same_503_symptom", "recent_change"]
        replays.append(replay)

    dns = _base_incident("INC-003")
    for idx in range(1, 5):
        replay = _clone(
            dns,
            f"REPLAY-DNS-{idx:03d}",
            f"DNS failure replay {idx}",
            f"Workers cannot resolve internal service names. NXDOMAIN and could not resolve host are increasing. replay={idx}",
            "DNSFailure",
        )
        replay["service"] = "worker"
        replay["metrics"]["dns_error_rate_percent"] = 10 + idx
        replay["logs_tail"] = dns["logs_tail"] + [f"ERROR could not resolve host replay={idx}"]
        replay["severity"] = "sev2"
        replay["evaluation_tags"] = ["stale_metric", "dns_logs"] if idx == 4 else ["dns"]
        replays.append(replay)

    unknown = _base_incident("INC-004")
    for idx in range(1, 5):
        replay = _clone(
            unknown,
            f"REPLAY-UNK-{idx:03d}",
            f"Ambiguous replay {idx}",
            f"Checkout feels slow but the signals are weak and inconsistent. replay={idx}",
            "Unknown",
            should_confirm=False,
        )
        replay["metrics"]["p95_latency_ms"] = 380 + (idx * 12)
        replay["metrics"]["error_rate_percent"] = 1 + (idx % 2)
        replay["logs_tail"] = unknown["logs_tail"] + [f"WARN retry observed replay={idx}"]
        replay["severity"] = "sev3"
        replay["evaluation_tags"] = ["ambiguous", "conflicting_signals", "must_escalate"]
        replays.append(replay)

    return replays
