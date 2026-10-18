from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List


def _candidate_hint(incident_type: str, weight: float) -> Dict[str, Any]:
    return {"incident_type": incident_type, "weight": round(float(weight), 3)}


def _build_finding(
    specialist: str,
    *,
    status: str,
    confidence: float,
    summary: str,
    observations: List[str],
    citations: List[str],
    recommended_next_tools: List[str] | None = None,
    candidate_hints: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "specialist": specialist,
        "status": status,
        "confidence": round(float(confidence), 3),
        "summary": summary.strip(),
        "observations": observations[:6],
        "citations": citations[:6],
        "recommended_next_tools": list(recommended_next_tools or [])[:3],
        "candidate_hints": list(candidate_hints or [])[:4],
    }


def _metric_finding(evidence: Dict[str, Any]) -> Dict[str, Any]:
    metrics = dict(evidence.get("metrics") or {})
    prometheus = dict(evidence.get("prometheus") or {})
    observations = []
    recommended = []
    candidate_hints: List[Dict[str, Any]] = []

    metric_map = {
        "error_rate_percent": "error rate",
        "dns_error_rate_percent": "DNS error rate",
        "p95_latency_ms": "p95 latency",
        "cpu_percent": "CPU",
        "memory_percent": "memory",
    }
    for key, label in metric_map.items():
        if key in metrics:
            observations.append(f"{label}={metrics[key]}")
    if prometheus.get("value") is not None:
        observations.append(f"prometheus_value={prometheus.get('value')}")

    if not metrics and not prometheus:
        return _build_finding(
            "metrics",
            status="missing",
            confidence=0.1,
            summary="Metrics have not been collected yet.",
            observations=[],
            citations=["tool:get_metrics", "tool:query_prometheus"],
            recommended_next_tools=["get_metrics", "query_prometheus"],
        )

    anomalies = []
    if float(metrics.get("dns_error_rate_percent", 0.0) or 0.0) >= 5:
        anomalies.append("DNS error rate is elevated.")
        candidate_hints.append(_candidate_hint("DNSFailure", 0.9))
    if float(metrics.get("error_rate_percent", 0.0) or 0.0) >= 5:
        anomalies.append("HTTP error rate is elevated.")
        candidate_hints.append(_candidate_hint("Service503", 0.75))
    if float(metrics.get("p95_latency_ms", 0.0) or 0.0) >= 1000:
        anomalies.append("Latency is elevated.")
        candidate_hints.append(_candidate_hint("DependencyLatency", 0.55))
        candidate_hints.append(_candidate_hint("Service503", 0.35))
    if float(metrics.get("cpu_percent", 0.0) or 0.0) >= 85:
        anomalies.append("CPU saturation is high.")
        candidate_hints.append(_candidate_hint("HighCPU", 0.9))
    if float(metrics.get("memory_percent", 0.0) or 0.0) >= 85:
        anomalies.append("Memory pressure is high.")
        candidate_hints.append(_candidate_hint("HighMemory", 0.85))
        candidate_hints.append(_candidate_hint("CrashLoopBackOff", 0.25))

    if anomalies:
        return _build_finding(
            "metrics",
            status="supporting",
            confidence=min(0.95, 0.45 + (0.12 * len(anomalies))),
            summary=" ".join(anomalies[:2]),
            observations=observations,
            citations=["tool:get_metrics", "tool:query_prometheus"],
            candidate_hints=candidate_hints,
        )

    return _build_finding(
        "metrics",
        status="neutral",
        confidence=0.35,
        summary="Metrics are present but do not show a dominant failure signal.",
        observations=observations,
        citations=["tool:get_metrics", "tool:query_prometheus"],
        recommended_next_tools=["get_pod_logs"],
    )


def _log_finding(evidence: Dict[str, Any]) -> Dict[str, Any]:
    log_lines = list(evidence.get("logs_tail") or [])
    loki_lines = list((evidence.get("loki") or {}).get("lines", []) or [])
    lines = log_lines or loki_lines
    lowered = " ".join(lines).lower()
    observations = lines[-3:]

    if not lines:
        return _build_finding(
            "logs",
            status="missing",
            confidence=0.1,
            summary="Logs have not been collected yet.",
            observations=[],
            citations=["tool:get_pod_logs", "tool:query_loki"],
            recommended_next_tools=["get_pod_logs", "query_loki"],
        )

    candidate_hints: List[Dict[str, Any]] = []
    if any(token in lowered for token in ["nxdomain", "could not resolve", "resolver", "dns"]):
        return _build_finding(
            "logs",
            status="supporting",
            confidence=0.92,
            summary="Logs show DNS lookup failures.",
            observations=observations,
            citations=["tool:get_pod_logs", "tool:query_loki"],
            candidate_hints=[_candidate_hint("DNSFailure", 0.95)],
        )
    if any(token in lowered for token in ["503", "upstream timeout", "bad gateway", "connection refused"]):
        return _build_finding(
            "logs",
            status="supporting",
            confidence=0.88,
            summary="Logs show upstream timeout or 503 behaviour.",
            observations=observations,
            citations=["tool:get_pod_logs", "tool:query_loki"],
            candidate_hints=[_candidate_hint("Service503", 0.9), _candidate_hint("DependencyLatency", 0.4)],
        )
    if any(token in lowered for token in ["oom", "out of memory", "oomkilled", "crashloop"]):
        return _build_finding(
            "logs",
            status="supporting",
            confidence=0.9,
            summary="Logs show crash loop or memory-related failures.",
            observations=observations,
            citations=["tool:get_pod_logs", "tool:query_loki"],
            candidate_hints=[_candidate_hint("CrashLoopBackOff", 0.92), _candidate_hint("HighMemory", 0.45)],
        )
    if any(token in lowered for token in ["x509", "certificate", "tls handshake"]):
        return _build_finding(
            "logs",
            status="supporting",
            confidence=0.87,
            summary="Logs show TLS or certificate failures.",
            observations=observations,
            citations=["tool:get_pod_logs", "tool:query_loki"],
            candidate_hints=[_candidate_hint("TLSCertificateExpiry", 0.9)],
        )

    candidate_hints.extend([_candidate_hint("Service503", 0.2), _candidate_hint("DNSFailure", 0.15)])
    return _build_finding(
        "logs",
        status="neutral",
        confidence=0.35,
        summary="Logs are available but do not show one dominant error pattern.",
        observations=observations,
        citations=["tool:get_pod_logs", "tool:query_loki"],
        candidate_hints=candidate_hints,
        recommended_next_tools=["get_cluster_events", "query_tempo"],
    )


def _dependency_finding(evidence: Dict[str, Any], service_memory: Dict[str, Any]) -> Dict[str, Any]:
    spans = list((evidence.get("tempo") or {}).get("spans", []) or [])
    dashboard_context = dict(evidence.get("dashboard_context") or {})
    dependencies = list(service_memory.get("dependencies") or [])
    observations = []
    candidate_hints: List[Dict[str, Any]] = []

    if dashboard_context.get("dashboard_title"):
        observations.append(f"dashboard={dashboard_context.get('dashboard_title')}")
    if dependencies:
        observations.append(f"dependencies={', '.join(str(item) for item in dependencies[:3])}")
    if spans:
        observations.extend(
            f"{span.get('span', 'span')}: {span.get('error', '') or 'ok'}"
            for span in spans[:2]
            if isinstance(span, dict)
        )

    if not spans and not dashboard_context and not dependencies:
        return _build_finding(
            "dependency",
            status="missing",
            confidence=0.1,
            summary="Dependency or trace context has not been collected yet.",
            observations=[],
            citations=["tool:query_tempo", "tool:get_dashboard_context"],
            recommended_next_tools=["query_tempo", "get_dashboard_context"],
        )

    lowered = " ".join(observations).lower()
    if any(token in lowered for token in ["deadline exceeded", "timeout", "upstream", "dependency"]):
        candidate_hints.extend([_candidate_hint("DependencyLatency", 0.85), _candidate_hint("Service503", 0.45)])
        return _build_finding(
            "dependency",
            status="supporting",
            confidence=0.82,
            summary="Trace or dashboard context points to an upstream dependency issue.",
            observations=observations,
            citations=["tool:query_tempo", "tool:get_dashboard_context"],
            candidate_hints=candidate_hints,
        )

    return _build_finding(
        "dependency",
        status="neutral",
        confidence=0.35,
        summary="Dependency context is available but does not show a clear upstream bottleneck.",
        observations=observations,
        citations=["tool:query_tempo", "tool:get_dashboard_context"],
    )


def _change_finding(evidence: Dict[str, Any], service_memory: Dict[str, Any]) -> Dict[str, Any]:
    deploys = list(evidence.get("recent_deploys") or service_memory.get("recent_deploys") or [])
    history = list(evidence.get("incident_history") or service_memory.get("incident_history") or [])
    owner = dict(evidence.get("service_owner") or service_memory.get("owner") or {})
    observations = []
    candidate_hints: List[Dict[str, Any]] = []

    if deploys:
        latest = deploys[0]
        observations.append(f"latest deploy {latest.get('version', 'unknown')} {latest.get('minutes_ago', 'n/a')}m ago")
    if history:
        prior = history[0]
        observations.append(f"prior {prior.get('type', 'unknown')} {prior.get('hours_ago', 'n/a')}h ago")
    if owner.get("team"):
        observations.append(f"owner={owner.get('team')}")

    if not deploys and not history and not owner:
        return _build_finding(
            "change",
            status="missing",
            confidence=0.1,
            summary="Change history and ownership context have not been collected yet.",
            observations=[],
            citations=["tool:get_recent_deploys", "tool:get_incident_history", "tool:get_service_owner"],
            recommended_next_tools=["get_recent_deploys", "get_incident_history", "get_service_owner"],
        )

    age = deploys[0].get("minutes_ago") if deploys else None
    recent_deploy = isinstance(age, (int, float)) and 0 <= age <= 30
    metrics = dict(evidence.get("metrics") or {})
    logs_text = " ".join(str(item) for item in (evidence.get("logs_tail") or [])).lower()
    strong_runtime_signal = (
        float(metrics.get("error_rate_percent", 0.0) or 0.0) >= 5
        or float(metrics.get("p95_latency_ms", 0.0) or 0.0) >= 1000
        or any(token in logs_text for token in ["503", "timeout", "regression", "exception", "connection refused", "nxdomain"])
    )

    if recent_deploy and strong_runtime_signal:
        candidate_hints.append(_candidate_hint("DeploymentRegression", 0.78))
        return _build_finding(
            "change",
            status="supporting",
            confidence=0.72,
            summary="A recent deploy strongly correlates with the alert window.",
            observations=observations,
            citations=["tool:get_recent_deploys", "tool:get_incident_history", "tool:get_service_owner"],
            candidate_hints=candidate_hints,
        )
    if recent_deploy:
        candidate_hints.append(_candidate_hint("DeploymentRegression", 0.28))
        return _build_finding(
            "change",
            status="neutral",
            confidence=0.32,
            summary="A recent deploy is worth checking, but current logs and metrics do not yet tie it to the failure.",
            observations=observations,
            citations=["tool:get_recent_deploys", "tool:get_incident_history", "tool:get_service_owner"],
            candidate_hints=candidate_hints,
            recommended_next_tools=["get_pod_logs", "get_metrics"],
        )

    if history:
        prior_type = str(history[0].get("type", "")).strip()
        if prior_type:
            candidate_hints.append(_candidate_hint(prior_type, 0.45))

    return _build_finding(
        "change",
        status="neutral",
        confidence=0.4,
        summary="Change history is available but does not show a recent deployment correlation.",
        observations=observations,
        citations=["tool:get_recent_deploys", "tool:get_incident_history", "tool:get_service_owner"],
        candidate_hints=candidate_hints,
    )


def build_specialist_findings(evidence: Dict[str, Any], service_memory: Dict[str, Any]) -> List[Dict[str, Any]]:
    service_payload = dict(service_memory or {})
    return [
        _metric_finding(evidence),
        _log_finding(evidence),
        _dependency_finding(evidence, service_payload),
        _change_finding(evidence, service_payload),
    ]


def coordinate_specialist_findings(findings: List[Dict[str, Any]], candidate_diagnoses: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    ranked = list(candidate_diagnoses or [])
    top_candidate = dict(ranked[0] if ranked else {})
    merged_scores: Dict[str, float] = defaultdict(float)
    supporting = []
    contradicting = []
    missing = []
    recommended_tools: List[str] = []
    activity = []
    confidence_total = 0.0

    for candidate in ranked[:5]:
        merged_scores[str(candidate.get("incident_type", "Unknown"))] += float(candidate.get("score", candidate.get("retrieval_confidence", 0.0)) or 0.0) * 0.45

    for finding in findings:
        specialist = str(finding.get("specialist", "unknown"))
        status = str(finding.get("status", "neutral"))
        confidence = float(finding.get("confidence", 0.0) or 0.0)
        summary = str(finding.get("summary", "")).strip()
        confidence_total += confidence
        if status == "supporting":
            supporting.append(summary)
        elif status == "missing":
            missing.append(summary)
        else:
            contradicting.append(summary)
        for tool_name in list(finding.get("recommended_next_tools") or []):
            if tool_name not in recommended_tools:
                recommended_tools.append(tool_name)
        for hint in list(finding.get("candidate_hints") or []):
            incident_type = str(hint.get("incident_type", "Unknown"))
            merged_scores[incident_type] += confidence * float(hint.get("weight", 0.0) or 0.0)
        activity.append(
            {
                "stage": specialist,
                "status": status,
                "summary": summary,
                "confidence": confidence,
                "citations": list(finding.get("citations") or []),
                "recommended_next_tools": list(finding.get("recommended_next_tools") or []),
            }
        )

    focus = "metrics"
    if any(item.get("specialist") == "logs" and item.get("status") == "supporting" for item in findings):
        focus = "logs"
    elif any(item.get("specialist") == "dependency" and item.get("status") == "supporting" for item in findings):
        focus = "dependency"
    elif any(item.get("specialist") == "change" and item.get("status") == "supporting" for item in findings):
        focus = "change"

    merged_ranked = [
        {"incident_type": incident_type, "score": round(score, 3)}
        for incident_type, score in sorted(merged_scores.items(), key=lambda item: item[1], reverse=True)
        if incident_type and incident_type != "Unknown"
    ]
    summary = "Specialists did not find a single dominant signal yet."
    if supporting:
        summary = supporting[0]
    elif missing:
        summary = missing[0]

    supporting_count = Counter(item.get("specialist", "unknown") for item in findings if item.get("status") == "supporting")
    reliability = "low"
    if len(supporting_count) >= 2 or (merged_ranked and merged_ranked[0]["score"] >= 0.8):
        reliability = "high"
    elif merged_ranked or supporting:
        reliability = "medium"

    return {
        "focus": focus,
        "summary": summary,
        "supporting_points": supporting[:4],
        "contradicting_points": contradicting[:3],
        "missing_domains": missing[:3],
        "recommended_next_tool": recommended_tools[0] if recommended_tools else "",
        "recommended_next_tools": recommended_tools[:3],
        "top_candidate": merged_ranked[0]["incident_type"] if merged_ranked else top_candidate.get("incident_type", "Unknown"),
        "candidate_rankings": merged_ranked[:4],
        "reliability": reliability,
        "confidence": round(confidence_total / max(len(findings), 1), 3),
        "activity": activity,
    }
