"""Scrape /metrics, tail server.log, detect breaches (PRD A4). Signal format: SIGNALS.md."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from prometheus_client.parser import text_string_to_metric_families

from factory import policy
from factory.audit import runs_dir
from factory.contracts import MetricsSnapshot

MIN_SAMPLES = 3          # fewer requests than this in a window never count as a breach
LOG_KEEP = 5000          # recent log lines kept per instance
FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')


def parse_ts(value: str | datetime | float) -> datetime:
    """ISO string, datetime or epoch seconds -> aware UTC datetime."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def app_frames(traceback: str) -> list[tuple[str, str]]:
    """(app/... file, function) for frames under APP_DIR, deepest first."""
    out = []
    for line in (traceback or "").splitlines():
        m = FRAME.match(line)
        if not m:
            continue
        path = m.group(1)
        if path.startswith(policy.APP_DIR):
            out.append((path, m.group(2)))
        elif f"/{policy.APP_DIR}" in path:
            out.append((path[path.rfind(f"/{policy.APP_DIR}") + 1:], m.group(2)))
    return out[::-1]


def signature(entry: dict) -> str:
    """'ExceptionType@app/file.py:function' (deepest app frame), else 'HTTP<status>@<route>'."""
    frames = app_frames(entry.get("traceback", ""))
    if entry.get("exception_type") and frames:
        file, func = frames[0]
        return f"{entry['exception_type']}@{file}:{func}"
    return f"HTTP{entry.get('status')}@{entry.get('route') or entry.get('path')}"


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


def _row(data: dict, path: str) -> Optional[dict]:
    """One route's counters, or all routes summed for path "*"."""
    if path != "*":
        return data.get(path)
    if not data:
        return None
    merged: dict = {"total": 0.0, "errors": 0.0, "buckets": {}}
    for row in data.values():
        merged["total"] += row["total"]
        merged["errors"] += row["errors"]
        for le, c in row["buckets"].items():
            merged["buckets"][le] = merged["buckets"].get(le, 0.0) + c
    return merged


class Watcher:
    """Thread-safe: one thread ticks (run_forever / start), others read. Reads tick themselves when stale."""

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
        self._lock = threading.RLock()
        self._last_tick: Optional[float] = None         # time.monotonic() of the last tick
        self._thread: Optional[threading.Thread] = None
        self.on_breach: Optional[Callable[[dict], Any]] = None

    @property
    def log_path(self) -> Path:
        return self._log_path or runs_dir() / "server.log"

    def add_target(self, instance: str, base_url: str) -> None:
        with self._lock:
            self.targets[instance] = base_url

    def remove_target(self, instance: str) -> None:
        with self._lock:
            self.targets.pop(instance, None)
            self.history.pop(instance, None)

    def _fresh(self) -> None:
        """Nobody ticking (stage run standalone)? Tick now so reads reflect the server."""
        if self._last_tick is None or time.monotonic() - self._last_tick > 2 * policy.SCRAPE_INTERVAL_S:
            self.tick()

    # --- one scrape interval ------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> list[dict]:
        """Scrape every target, tail the log, return breaches (stable only) at this tick."""
        with self._lock:
            self._last_tick = time.monotonic()
            return self._tick(parse_ts(now) if now else datetime.now(timezone.utc))

    def _tick(self, now: datetime) -> list[dict]:
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
                snap = self._current(route, instance)
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

    def start(self, on_breach: Optional[Callable[[dict], Any]] = None) -> "Watcher":
        """Tick in a daemon thread (once per process); breaches go to on_breach (replaces the previous one)."""
        with self._lock:
            if on_breach is not None:
                self.on_breach = on_breach
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self.run_forever, name="watcher", daemon=True,
                    args=(threading.Event(), lambda b: self.on_breach and self.on_breach(b)))
                self._thread.start()
        return self

    # --- metrics views --------------------------------------------------------
    def current(self, path: str, instance: str = "stable") -> MetricsSnapshot:
        """Metrics for route `path` ("*" = all routes) between the oldest and newest scrape in the window."""
        with self._lock:
            self._fresh()
            return self._current(path, instance)

    def _current(self, path: str, instance: str) -> MetricsSnapshot:
        hist = self.history.get(instance)
        snap: MetricsSnapshot = {"metrics": {}, "instance": instance, "path": path, "window_s": 0.0, "samples": 0}
        if not hist or len(hist) < 2:
            return snap
        (t_old, old), (t_new, new) = hist[0], hist[-1]   # tick() keeps hist[0] at about now - ALERT_WINDOW_S
        row_new = _row(new, path)
        if not row_new:
            return snap
        row_old = _row(old, path) or {"total": 0.0, "errors": 0.0, "buckets": {}}
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
        with self._lock:
            self._fresh()
            found = self._baselines.get((instance, path))
        return found or {
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
        with self._lock:
            self._fresh()
            rows = [
                (ts, e)
                for inst, q in self.logs.items() if instance in (None, inst)
                for ts, e in q
                if ts >= lo and (hi is None or ts <= hi) and route in (None, e.get("route"))
            ]
        return [e for _, e in sorted(rows, key=lambda r: r[0])]

    def signatures(self, since_ts: str | datetime | float, instance: str = "stable",
                   until_ts: str | datetime | float | None = None) -> Counter:
        """Signature counts of 5xx log lines on `instance` with since_ts <= ts <= until_ts (default: now)."""
        return Counter(signature(e) for e in self.log_lines(since_ts, until_ts, instance=instance)
                       if int(e.get("status", 0)) >= 500)


_DEFAULT: Optional[Watcher] = None
_DEFAULT_LOCK = threading.Lock()


def default() -> Watcher:
    """Process-wide Watcher, ticking in a daemon thread. Stages use it when no watcher is injected."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Watcher()
        return _DEFAULT.start()
