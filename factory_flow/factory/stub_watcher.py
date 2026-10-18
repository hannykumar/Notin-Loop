# STUB(track A): replace with factory.watcher (A4) at T1
"""Minimal log-based Watcher for B5/B6 until Track A's watcher (A4) lands.

Implements the frozen contracts.Watcher protocol (baseline / current -> MetricsSnapshot) by reading
runs/server.log (one JSON line per request, written by target_service/app/sre_plugin.py), plus the
extra helper the promote/rollback stages need:

    signatures(since_ts, instance, until_ts=None) -> Counter["ExceptionType@app/file.py:function"]

A4 must provide the same three calls (current/baseline with the optional keyword arguments are
used only by this stub's callers in tests; stages call them with (path, instance) only).

Window semantics (all timestamps are epoch seconds):
    current(path, instance, until=None, window_s=ALERT_WINDOW_S)   -> requests in (until - window_s, until]
    baseline(path, instance, until=None, window_s=ALERT_WINDOW_S)  -> the window ending at `until`
                                                                       (default: the window before current())
    path "*" means all routes; otherwise it matches the route template or the concrete path.
    samples == 0 means no evidence: callers must treat that as "unsure".

Patterned on SRE-agent/executor/engine.py @ b4f7e0c (collect_evidence returns {"metrics": {...}} so the
snapshot can be passed straight to classify_verification_outcome). New code: log parsing.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from factory.contracts import MetricsSnapshot
from factory.policy import ALERT_WINDOW_S, APP_DIR

FACTORY_FLOW_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG = FACTORY_FLOW_DIR / "runs" / "server.log"

_FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\S+)')


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def app_relative(file_path: str) -> Optional[str]:
    """'/tmp/sre-wt-x/target_service/app/orders.py' -> 'app/orders.py'; None if not under app/."""
    file_path = file_path.replace("\\", "/")
    if file_path.startswith(APP_DIR):
        return file_path
    idx = file_path.rfind("/" + APP_DIR)
    return file_path[idx + 1:] if idx >= 0 else None


def signature_of(entry: dict) -> Optional[str]:
    """'ExceptionType@app/file.py:function' from the innermost traceback frame under app/."""
    exc = entry.get("exception_type")
    if not exc:
        return None
    frames = [(app_relative(f), fn) for f, fn in _FRAME_RE.findall(entry.get("traceback") or "")]
    frames = [(f, fn) for f, fn in frames if f]
    if not frames:
        return f"{exc}@?"
    file_, func = frames[-1]
    return f"{exc}@{file_}:{func}"


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return float(ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)])


class StubWatcher:
    def __init__(self, log_path: Path | str | None = None, clock=time.time):
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG
        self.clock = clock

    # -- raw access ---------------------------------------------------------
    def entries(self, since_ts: float, until_ts: float, instance: Optional[str] = None) -> Iterator[dict]:
        """Log entries with since_ts < ts <= until_ts (and the given instance, if any)."""
        try:
            handle = self.log_path.open(encoding="utf-8")
        except FileNotFoundError:
            return
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                    ts = parse_ts(entry["ts"])
                except (ValueError, KeyError, TypeError):
                    continue
                if since_ts < ts <= until_ts and (instance is None or entry.get("instance") == instance):
                    entry["_ts"] = ts
                    yield entry

    def snapshot(self, path: str, instance: str, since_ts: float, until_ts: float) -> MetricsSnapshot:
        rows = [e for e in self.entries(since_ts, until_ts, instance)
                if path == "*" or path in (e.get("route"), e.get("path"))]
        metrics: dict[str, float] = {}
        if rows:
            errors = sum(1 for e in rows if int(e.get("status", 0)) >= 500)
            metrics = {
                "error_rate_percent": round(100.0 * errors / len(rows), 3),
                "p95_latency_ms": p95([float(e.get("latency_ms", 0.0)) for e in rows]),
            }
        return MetricsSnapshot(metrics=metrics, instance=instance, path=path,  # type: ignore[typeddict-item]
                               window_s=round(until_ts - since_ts, 3), samples=len(rows))

    # -- Watcher protocol ---------------------------------------------------
    def current(self, path: str, instance: str = "stable", *, until: Optional[float] = None,
                window_s: float = ALERT_WINDOW_S) -> MetricsSnapshot:
        until = self.clock() if until is None else until
        return self.snapshot(path, instance, until - window_s, until)

    def baseline(self, path: str, instance: str = "stable", *, until: Optional[float] = None,
                 window_s: float = ALERT_WINDOW_S) -> MetricsSnapshot:
        until = self.clock() - window_s if until is None else until
        return self.snapshot(path, instance, until - window_s, until)

    # -- extra helper (not in the frozen protocol) ----------------------------
    def signatures(self, since_ts: float, instance: str = "stable",
                   until_ts: Optional[float] = None) -> Counter:
        until_ts = self.clock() if until_ts is None else until_ts
        found: Counter = Counter()
        for entry in self.entries(since_ts, until_ts, instance):
            if int(entry.get("status", 0)) >= 500:
                found[signature_of(entry) or f"HTTP{entry.get('status')}@{entry.get('route')}"] += 1
        return found


_DEFAULT: Optional[StubWatcher] = None


def default_watcher() -> StubWatcher:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = StubWatcher()
    return _DEFAULT


def signatures(since_ts: float, instance: str = "stable", until_ts: Optional[float] = None) -> Counter:
    return default_watcher().signatures(since_ts, instance, until_ts)


def watcher_signatures(watcher: Any, since_ts: float, instance: str = "stable",
                       until_ts: Optional[float] = None) -> Counter:
    """Use watcher.signatures if the injected watcher has it, else the stub's log reader."""
    fn = getattr(watcher, "signatures", None)
    return fn(since_ts, instance, until_ts) if callable(fn) else signatures(since_ts, instance, until_ts)
