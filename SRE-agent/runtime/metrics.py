from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
except Exception:  # pragma: no cover - optional dependency fallback
    class _NullMetric:
        def labels(self, **_: str) -> "_NullMetric":
            return self

        def inc(self, amount: float = 1.0) -> None:
            return None

        def dec(self, amount: float = 1.0) -> None:
            return None

        def set(self, value: float) -> None:
            return None

        def observe(self, value: float) -> None:
            return None

    Counter = Gauge = Histogram = lambda *args, **kwargs: _NullMetric()  # type: ignore

    def generate_latest() -> bytes:
        return b""

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

REQUEST_COUNTER = Counter("sre_agent_requests_total", "Total API requests", ["endpoint", "outcome"])
QUEUE_DEPTH = Gauge("sre_agent_queue_depth", "Current queued jobs")
ACTIVE_JOBS = Gauge("sre_agent_active_jobs", "Currently executing jobs")
PLAN_DURATION = Histogram("sre_agent_plan_duration_seconds", "End-to-end planning latency")
EXECUTION_DURATION = Histogram("sre_agent_execution_duration_seconds", "End-to-end execution latency")
TOOL_DURATION = Histogram("sre_agent_tool_duration_seconds", "Tool call latency", ["tool_name"])
RETRIEVAL_DURATION = Histogram("sre_agent_retrieval_duration_seconds", "Retrieval latency")
PLANNER_DURATION = Histogram("sre_agent_planner_duration_seconds", "Planner latency")
QUEUE_WAIT_DURATION = Histogram("sre_agent_queue_wait_seconds", "Queue waiting latency")


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


@contextmanager
def observe_duration(histogram: Histogram, **labels: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = max(time.perf_counter() - start, 0.0)
        if labels:
            histogram.labels(**labels).observe(elapsed)
        else:
            histogram.observe(elapsed)


def observe_value(histogram: Histogram, value: float, **labels: str) -> None:
    if labels:
        histogram.labels(**labels).observe(value)
    else:
        histogram.observe(value)
