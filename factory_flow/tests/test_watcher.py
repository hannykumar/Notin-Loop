"""A4: replay sample_server.log through the watcher with a fake /metrics endpoint."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from factory import policy
from factory.watcher import Watcher, parse_ts

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "sample_server.log"
T0 = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)
ENTRIES = [json.loads(line) for line in FIXTURE_LOG.read_text().splitlines()]


def metrics_at(entries: list[dict], now: datetime, instance: str) -> str:
    """/metrics text a SIGNALS.md server would serve at `now`, built from the log lines seen so far."""
    reg = CollectorRegistry()
    req = Counter("http_requests_total", "requests", ["instance", "method", "route", "status"], registry=reg)
    lat = Histogram("http_request_duration_seconds", "latency", ["instance", "route"], registry=reg)
    for e in entries:
        if e["instance"] == instance and parse_ts(e["ts"]) <= now:
            req.labels(instance, e["method"], e["route"], str(e["status"])).inc()
            lat.labels(instance, e["route"]).observe(e["latency_ms"] / 1000)
    return generate_latest(reg).decode()


class FakeServer:
    def __init__(self, entries: list[dict]):
        self.entries, self.now = entries, T0

    def get(self, url: str) -> str:
        instance = "canary" if f":{policy.CANARY_PORT}" in url else "stable"
        return metrics_at(self.entries, self.now, instance)


def replay(until_s: int = 140, entries: list[dict] = ENTRIES, log_path: Path = FIXTURE_LOG, targets=None):
    """Drive tick() every SCRAPE_INTERVAL_S from T0; yield (seconds, watcher, breaches) after each tick."""
    server = FakeServer(entries)
    watcher = Watcher(targets=targets, log_path=log_path, http_get=server.get)
    for s in range(0, until_s + 1, policy.SCRAPE_INTERVAL_S):
        server.now = T0 + timedelta(seconds=s)
        yield s, watcher, watcher.tick(server.now)


def test_cart_burst_breach_baseline_and_current():
    first_cart = None
    for s, w, breaches in replay(100):
        if s < 60:
            assert breaches == [], f"false alarm at t={s}"
        cart = [b for b in breaches if b["route"] == "/cart/total"]
        if cart and first_cart is None:
            first_cart = (s, cart[0])
            base = w.baseline("/cart/total")
            assert base["metrics"]["error_rate_percent"] == 0 and base["samples"] > 0
        if s == 80:
            cur = w.current("/cart/total")
            assert cur["metrics"]["error_rate_percent"] == 100.0 and cur["samples"] >= 3
            assert cur["window_s"] == policy.ALERT_WINDOW_S and "p95_latency_ms" in cur["metrics"]
            assert w.current("/orders/{order_id}")["metrics"]["error_rate_percent"] == 0
    assert first_cart is not None
    s, breach = first_cart
    assert 60 <= s <= 70
    assert breach["instance"] == "stable" and breach["samples"] >= 3
    assert parse_ts(breach["started_at"]) >= T0 + timedelta(seconds=60)
    # baseline stays frozen at the pre-breach window
    assert w.baseline("/cart/total")["metrics"]["error_rate_percent"] == 0


def test_no_evidence_is_empty_snapshot():
    w = Watcher(http_get=lambda url: "")
    snap = w.current("/cart/total")
    assert snap == {"metrics": {}, "instance": "stable", "path": "/cart/total", "window_s": 0.0, "samples": 0}
    assert w.baseline("/cart/total")["samples"] == 0


def test_canary_kept_separate_and_never_alerts(tmp_path):
    canary = [dict(e, instance="canary", status=500) for e in ENTRIES if e["route"] == "/orders/{order_id}"]
    entries = ENTRIES + canary
    log = tmp_path / "server.log"
    log.write_text("".join(json.dumps(e) + "\n" for e in entries))
    targets = {"stable": f"http://127.0.0.1:{policy.STABLE_PORT}", "canary": f"http://127.0.0.1:{policy.CANARY_PORT}"}
    for s, w, breaches in replay(30, entries=entries, log_path=log, targets=targets):
        assert breaches == []   # canary is all 500s on /orders, stable is healthy: no alert
    assert w.current("/orders/{order_id}", "canary")["metrics"]["error_rate_percent"] == 100.0
    assert w.current("/orders/{order_id}", "stable")["metrics"]["error_rate_percent"] == 0
    assert {e["instance"] for e in w.log_lines(T0, instance="canary")} == {"canary"}
    assert all(e["instance"] == "stable" for e in w.log_lines(T0, instance="stable"))
    w.remove_target("canary")
    assert w.current("/orders/{order_id}", "canary")["samples"] == 0


def test_log_lines_filters_and_tails_incrementally(tmp_path):
    log = tmp_path / "server.log"
    lines = FIXTURE_LOG.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:10]) + lines[10][:20])   # last line half-written
    w = Watcher(log_path=log, http_get=lambda url: "")
    w.tick(T0)
    assert len(w.log_lines(T0)) == 10
    with log.open("a") as fh:
        fh.write(lines[10][20:] + "".join(lines[11:]))
    w.tick(T0)
    assert len(w.log_lines(T0)) == len(lines)
    burst = w.log_lines("2026-10-18T14:01:00Z", "2026-10-18T14:01:09Z", route="/cart/total")
    assert len(burst) == 10 and all(e["status"] == 500 and e["exception_type"] == "KeyError" for e in burst)
    assert all(e["route"] == "/orders/{order_id}" for e in w.log_lines(T0, T0 + timedelta(seconds=5), route="/orders/{order_id}"))
    log.write_text(lines[0])   # truncated (reset.sh) => re-read from the start
    w.tick(T0)
    assert len(w.log_lines(T0)) == len(lines) + 1


def test_failing_scrape_is_recorded_not_raised(tmp_path):
    def boom(url):
        raise ConnectionError("refused")
    w = Watcher(log_path=tmp_path / "missing.log", http_get=boom)
    assert w.tick(T0) == []
    assert "ConnectionError" in w.scrape_errors["stable"]["error"]
    assert w.current("/cart/total")["samples"] == 0
