"""Scrape /metrics, tail server.log, detect breaches (PRD A4). Signal format: SIGNALS.md."""
from __future__ import annotations

import json
import math
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from prometheus_client.parser import text_string_to_metric_families

from factory import policy
from factory.contracts import MetricsSnapshot
from factory.sre_base import FACTORY_FLOW_DIR

MIN_SAMPLES = 3          # fewer requests than this in a window never count as a breach
LOG_KEEP = 5000          # recent log lines kept per instance


def runs_dir() -> Path:
    return Path(os.environ.get("FACTORY_RUNS_DIR", FACTORY_FLOW_DIR / "runs"))


def parse_ts(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_get(url: str) -> str:
    resp = httpx.get(url, timeout=1.5)
    resp.raise_for_status()
    return resp.text


def parse_metrics(text: str) -> dict[str, dict]:
    """route -> {"total", "errors", "buckets": {le: cumulative count}} from SIGNALS.md metric names."""
    out: dict[str, dict] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            route = s.labels.get("route")
            if route is None:
                continue
            row = out.setdefault(route, {"total": 0.0, "errors": 0.0, "buckets": {}})
            if s.name == "http_requests_total":
                row["total"] += s.value
                if int(s.labels.get("status", "0")) >= 500:
                    row["errors"] += s.value
            elif s.name == "http_request_duration_seconds_bucket":
                le = float(s.labels["le"])
                row["buckets"][le] = row["buckets"].get(le, 0.0) + s.value
    return out


def _delta(new: float, old: float) -> float:
    return new if new < old else new - old   # counter reset (restart) => count from zero


def _p95_ms(new: dict, old: dict) -> Optional[float]:
    deltas = sorted((le, _delta(c, old.get(le, 0.0))) for le, c in new.items())
    if not deltas or deltas[-1][1] <= 0:
        return None
    target = 0.95 * deltas[-1][1]
    finite = [le for le, _ in deltas if not math.isinf(le)]
    for le, count in deltas:
        if count >= target:
            # ponytail: +Inf bucket reported as the largest finite bound (a lower bound); fine for SLO caps
            return round((le if not math.isinf(le) else (finite[-1] if finite else 0.0)) * 1000, 3)
    return None


class Watcher:
    def __init__(
        self,
        targets: Optional[dict[str, str]] = None,
        log_path: Optional[Path] = None,
        http_get: Callable[[str], str] = _default_get,
    ):
        self.targets = dict(targets or {"stable": f"http://127.0.0.1:{policy.STABLE_PORT}"})
        self._log_path = Path(log_path) if log_path else None
        self.http_get = http_get
        self.history: dict[str, deque] = {}            # instance -> deque[(ts, parse_metrics())]
        self.scrape_errors: dict[str, dict] = {}        # instance -> {"at", "error"} of the last failure
        self._baselines: dict[tuple[str, str], MetricsSnapshot] = {}
        self._streaks: dict[str, tuple[int, datetime]] = {}   # stable route -> (breached ticks, first breached tick)
        self._log_offset = 0
        self._log_buf = ""
        self.logs: dict[str, deque] = {}                # instance -> deque[(ts, entry)]

    @property
    def log_path(self) -> Path:
        return self._log_path or runs_dir() / "server.log"

    def add_target(self, instance: str, base_url: str) -> None:
        self.targets[instance] = base_url

    def remove_target(self, instance: str) -> None:
        self.targets.pop(instance, None)
        self.history.pop(instance, None)

    # --- one scrape interval ------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> list[dict]:
        """Scrape every target, tail the log, return breaches (stable only) at this tick."""
        now = parse_ts(now) if now else datetime.now(timezone.utc)
        for instance, base in list(self.targets.items()):
            try:
                data = parse_metrics(self.http_get(base.rstrip("/") + "/metrics"))
            except Exception as exc:  # never crash the loop; a broken window is no evidence
                self.scrape_errors[instance] = {"at": iso(now), "error": f"{type(exc).__name__}: {exc}"}
                self.history.pop(instance, None)
                continue
            hist = self.history.setdefault(instance, deque())
            hist.append((now, data))
            while len(hist) > 2 and hist[1][0] <= now - timedelta(seconds=policy.ALERT_WINDOW_S):
                hist.popleft()
        self._tail_log()
        return self._evaluate(now)

    def _evaluate(self, now: datetime) -> list[dict]:
        breaches = []
        for instance, hist in self.history.items():
            for route in hist[-1][1]:
                snap = self.current(route, instance)
                if not snap["samples"]:
                    if instance == "stable":
                        self._streaks.pop(route, None)   # no traffic breaks the streak
                    continue
                rate = snap["metrics"]["error_rate_percent"]
                if rate < policy.ALERT_ERROR_RATE_PERCENT:
                    self._baselines[(instance, route)] = snap
                if instance != "stable":
                    continue
                if rate >= policy.ALERT_ERROR_RATE_PERCENT and snap["samples"] >= MIN_SAMPLES:
                    count, started = self._streaks.get(route, (0, now))
                    self._streaks[route] = (count + 1, started)
                    if count + 1 >= policy.ALERT_CONSECUTIVE_WINDOWS:
                        breaches.append({
                            "route": route, "instance": instance,
                            "error_rate_percent": rate,
                            "p95_latency_ms": snap["metrics"].get("p95_latency_ms"),
                            "samples": snap["samples"],
                            "started_at": iso(started), "detected_at": iso(now),
                        })
                else:
                    self._streaks.pop(route, None)
        return breaches

    def run_forever(self, stop_event: threading.Event, on_breach: Callable[[dict], Any]) -> None:
        while not stop_event.is_set():
            try:
                for breach in self.tick():
                    on_breach(breach)
            except Exception as exc:  # a bad tick or callback must not stop watching
                self.scrape_errors["_loop"] = {"at": iso(datetime.now(timezone.utc)), "error": repr(exc)}
            stop_event.wait(policy.SCRAPE_INTERVAL_S)

    # --- metrics views --------------------------------------------------------
    def current(self, path: str, instance: str = "stable") -> MetricsSnapshot:
        """Metrics for route `path` between the oldest and newest scrape in the sliding window."""
        hist = self.history.get(instance)
        snap: MetricsSnapshot = {"metrics": {}, "instance": instance, "path": path, "window_s": 0.0, "samples": 0}
        if not hist or len(hist) < 2:
            return snap
        (t_old, old), (t_new, new) = hist[0], hist[-1]   # tick() keeps hist[0] at about now - ALERT_WINDOW_S
        row_new = new.get(path)
        if not row_new:
            return snap
        row_old = old.get(path, {"total": 0.0, "errors": 0.0, "buckets": {}})
        total = _delta(row_new["total"], row_old["total"])
        snap["window_s"] = (t_new - t_old).total_seconds()
        snap["samples"] = int(total)
        if total > 0:
            errors = _delta(row_new["errors"], row_old["errors"])
            snap["metrics"]["error_rate_percent"] = round(100.0 * errors / total, 3)
            p95 = _p95_ms(row_new["buckets"], row_old["buckets"])
            if p95 is not None:
                snap["metrics"]["p95_latency_ms"] = p95
        return snap

    def baseline(self, path: str, instance: str = "stable") -> MetricsSnapshot:
        """Most recent healthy window for the route (frozen while it is breached); samples=0 if none."""
        return self._baselines.get((instance, path)) or {
            "metrics": {}, "instance": instance, "path": path, "window_s": 0.0, "samples": 0}

    # --- log tail ------------------------------------------------------------------
    def _tail_log(self) -> None:
        path = self.log_path
        try:
            size = path.stat().st_size
            if size < self._log_offset:          # truncated / reset
                self._log_offset, self._log_buf = 0, ""
            with path.open("rb") as fh:
                fh.seek(self._log_offset)
                chunk = fh.read()
        except OSError:
            return
        self._log_offset += len(chunk)
        *lines, self._log_buf = (self._log_buf + chunk.decode("utf-8", "replace")).split("\n")
        for line in lines:
            try:
                entry = json.loads(line)
                ts = parse_ts(entry["ts"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            self.logs.setdefault(entry.get("instance", "stable"), deque(maxlen=LOG_KEEP)).append((ts, entry))

    def log_lines(
        self,
        since: str | datetime,
        until: str | datetime | None = None,
        instance: Optional[str] = None,
        route: Optional[str] = None,
    ) -> list[dict]:
        """Tailed log entries with since <= ts <= until, optionally for one instance and route, in time order."""
        lo, hi = parse_ts(since), parse_ts(until) if until else None
        rows = [
            (ts, e)
            for inst, q in self.logs.items() if instance in (None, inst)
            for ts, e in q
            if ts >= lo and (hi is None or ts <= hi) and route in (None, e.get("route"))
        ]
        return [e for _, e in sorted(rows, key=lambda r: r[0])]
