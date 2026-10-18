"""Build, normalise and dedupe alerts (PRD A5)."""
from __future__ import annotations

from typing import Any, Callable, Optional

from factory import policy
from factory.sre_base import canonical_hash, normalize_generic_alert
from factory.watcher import parse_ts

ALERTNAME = "HighErrorRate"


def build_payload(breach: dict) -> dict:
    """Generic alert payload (the shape SRE-agent's /alerts/events accepts) from a watcher breach."""
    route = breach["route"]
    duration = (parse_ts(breach["detected_at"]) - parse_ts(breach["started_at"])).total_seconds()
    duration += policy.SCRAPE_INTERVAL_S   # the first breached tick covers one interval too
    return {
        "alertname": ALERTNAME,
        "service": "target_service",
        "severity": "page",
        "status": "firing",
        "starts_at": breach["started_at"],
        "labels": {"path": route, "instance": breach["instance"]},
        "summary": f"5xx rate {breach['error_rate_percent']:g}% on {route} for {duration:g} s",
        "fingerprint": canonical_hash({"alertname": ALERTNAME, "path": route}),
        # explicit: normalize_generic_alert would infer "demo_service_high_5xx" from "5xx" in the summary
        "scenario": "code_bug",
    }


def normalize(breach: dict) -> dict:
    alert = normalize_generic_alert(build_payload(breach))
    alert["breach"] = dict(breach)
    return alert


class AlertIntake:
    """One active run per fingerprint; repeats while it runs are dropped.

    Patterned on SRE-agent/runtime/api.py ingest_grafana_alert: an active run for the alert => "duplicate_alert", no new run.
    """

    def __init__(self, on_alert: Callable[[dict], Any]):
        self.on_alert = on_alert
        self.active: set[str] = set()   # ponytail: no lock; handle() is called from the single watcher thread

    def handle(self, breach: dict) -> Optional[dict]:
        alert = normalize(breach)
        fingerprint = alert["fingerprint"]
        if fingerprint in self.active:
            return None
        self.active.add(fingerprint)
        try:
            self.on_alert(alert)
        except Exception:
            self.active.discard(fingerprint)   # a crashed hand-off must not silence the route forever
            raise
        return alert

    def done(self, fingerprint: str) -> None:
        self.active.discard(fingerprint)
