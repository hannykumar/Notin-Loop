"""A5: payload, normalisation and dedupe; one cart burst through the watcher => one alert."""
from __future__ import annotations

from factory.alerts import AlertIntake, build_payload, normalize
from test_watcher import replay

BREACH = {"route": "/cart/total", "instance": "stable", "error_rate_percent": 100.0, "p95_latency_ms": 25.0,
          "samples": 10, "started_at": "2026-10-18T14:01:00Z", "detected_at": "2026-10-18T14:01:04Z"}


def test_normalized_alert_shape():
    alert = normalize(BREACH)
    assert alert["incident_id"].startswith("ALERT-")
    assert alert["fingerprint"] and alert["scenario"] == "code_bug"
    assert alert["labels"]["path"] == "/cart/total" and alert["labels"]["instance"] == "stable"
    assert alert["labels"]["service"] == "target_service" and alert["alertname"] == "HighErrorRate"
    assert alert["summary"] == "5xx rate 100% on /cart/total for 6 s"
    assert alert["breach"] == BREACH
    # fingerprint is per route, not per streak
    assert build_payload(dict(BREACH, started_at="2026-10-18T15:00:00Z"))["fingerprint"] == alert["fingerprint"]


def test_sustained_break_one_alert_until_done():
    calls = []
    intake = AlertIntake(calls.append)
    results = [intake.handle(dict(BREACH, detected_at=f"2026-10-18T14:01:{s:02d}Z")) for s in range(4, 40, 2)]
    assert len(calls) == 1 and results[0] is calls[0] and all(r is None for r in results[1:])
    other = intake.handle(dict(BREACH, route="/orders/{order_id}"))
    assert other and other["fingerprint"] != calls[0]["fingerprint"] and len(calls) == 2
    intake.done(calls[0]["fingerprint"])
    assert intake.handle(BREACH) is not None and len(calls) == 3


def test_failed_handoff_does_not_block_route():
    def boom(alert):
        raise RuntimeError("pipeline down")
    intake = AlertIntake(boom)
    try:
        intake.handle(BREACH)
    except RuntimeError:
        pass
    assert intake.active == set()


def test_cart_burst_through_watcher_gives_one_alert():
    calls = []
    intake = AlertIntake(calls.append)
    for s, w, breaches in replay(100):   # 60-100 s: cart burst only
        for breach in breaches:
            intake.handle(breach)
    assert len(calls) == 1
    assert calls[0]["labels"]["path"] == "/cart/total" and calls[0]["scenario"] == "code_bug"
