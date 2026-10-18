"""B5 unit checks: ledger, hash refusal, already-released refusal, claims, stub watcher, SLO judgement, watch.

The live end-to-end validation (real ports and policy windows) is tests/live_b5.py.
"""
import json
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory import releases, workspace
from factory.contracts import BugReport, Release, RunState
from factory.policy import ROLLBACK_LOOKBACK_S
from factory.sre_base import canonical_hash
from factory.stages import canary, promote
from factory.stub_watcher import StubWatcher, signature_of

CART_FIX = (workspace.TARGET_DIR / "patches" / "cart_fix.diff").read_text()
ORDERS_FIX = (workspace.TARGET_DIR / "patches" / "orders_helper_fix.diff").read_text()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "LEDGER_PATH", tmp_path / "releases.json")
    monkeypatch.setattr(promote, "CLAIMS_DIR", tmp_path / "claims")


def rel(tag: str, status: str = "kept", promoted_ago_s: float = 3600, sha: str = "") -> Release:
    ts = time.time() - promoted_ago_s
    return Release(tag=tag, commit="c" * 40, previous_tag="release-0", diff_sha256=sha or f"h-{tag}",
                   run_id=f"run-{tag}", promoted_at=releases.utc_iso(ts), watch_until=releases.utc_iso(ts + 45),
                   status=status, files_touched=["app/cart.py"], baseline={})


def state(diff: str = CART_FIX, sha: str | None = None, signature: str = "KeyError@app/cart.py:cart_total") -> RunState:
    bug = BugReport(id="BUG-1", alert_id="A-1", signature=signature, exception_type=signature.split("@")[0],
                    traceback="", sample_request={"method": "GET", "path": "/cart/total"}, files=["app/cart.py"],
                    error_share=1.0, replay_failures=3, evidence_ids=["E1", "E2"])
    return RunState(run_id="run-t", alert={}, artifacts_dir=Path("/tmp"), bug=bug, diff=diff,
                    diff_sha256=canonical_hash(diff) if sha is None else sha)


# ---------------------------------------------------------------- ledger

def test_ledger_contract_format_and_queries():
    assert releases.load() == [] and releases.latest() is None
    assert releases.previous_tag() == "release-0" and releases.next_tag() == "release-1"
    releases.append(rel("release-1"))
    releases.append(rel("release-2", status="watching", promoted_ago_s=5))
    raw = json.loads(releases.LEDGER_PATH.read_text())
    assert list(raw) == ["releases"] and raw["releases"][0] == asdict(releases.load()[0])
    assert releases.latest().tag == "release-2"
    assert releases.previous_tag() == "release-2"
    assert releases.next_tag(existing_tags=["release-0", "release-7"]) == "release-8"
    assert releases.in_window().tag == "release-2"
    assert releases.by_hash("h-release-1").tag == "release-1" and releases.by_hash("nope") is None
    with pytest.raises(ValueError):
        releases.append(rel("release-2"))

    releases.update("release-2", status="rolled_back")
    assert releases.previous_tag() == "release-1"
    assert releases.in_window() is None                       # release-1 is an hour old, release-2 rolled back
    releases.append(rel("release-3", status="kept", promoted_ago_s=ROLLBACK_LOOKBACK_S - 10))
    assert releases.in_window().tag == "release-3"            # kept but inside the lookback
    assert releases.in_window(time.time() + 60) is None
    with pytest.raises(KeyError):
        releases.update("release-9", status="kept")


# ---------------------------------------------------------------- hash and claim refusals

def test_canary_refuses_hash_mismatch_without_starting_anything(monkeypatch):
    monkeypatch.setattr(workspace, "fresh_worktree", lambda ref: pytest.fail("must not create a worktree"))
    v = canary.run(state(sha="0" * 64))
    assert v.stage == "canary" and v.status == "fail" and v.checks == {"hash_matches": False}
    assert "mismatch" in v.reasons[0]
    v = canary.run(state(sha=""))
    assert v.status == "fail" and "no diff_sha256" in v.reasons[0]


def test_promote_refuses_hash_mismatch():
    v = promote.run(state(sha="0" * 64))
    assert v.stage == "promote" and v.status == "fail" and v.checks["hash_matches"] is False


def test_promote_refuses_already_released_hash(monkeypatch):
    releases.append(rel("release-1", sha=canonical_hash(CART_FIX)))
    monkeypatch.setattr(promote, "claim", lambda *a: pytest.fail("must refuse before claiming"))
    v = promote.run(state())
    assert v.status == "fail" and v.checks["not_already_released"] is False
    assert "already released as release-1" in v.reasons[0]


class FakeWatcher:
    def __init__(self, snapshots=None, sigs=None):
        self.snapshots = snapshots or {}
        self.sigs = sigs or []          # list of (ts, signature)

    def current(self, path, instance="stable"):
        return self.snapshots.get(instance, {"metrics": {"error_rate_percent": 0.0, "p95_latency_ms": 2.0},
                                             "instance": instance, "path": path, "window_s": 10, "samples": 50})

    def baseline(self, path, instance="stable"):
        return self.current(path, instance)

    def signatures(self, since_ts, instance="stable", until_ts=None):
        until_ts = time.time() if until_ts is None else until_ts
        return Counter(s for ts, s in self.sigs if since_ts < ts <= until_ts)


def test_claim_never_runs_twice_and_promote_refuses_claimed_hash(monkeypatch):
    sha = canonical_hash(CART_FIX)
    assert promote.claim(sha, "run-a") is None
    again = promote.claim(sha, "run-b")
    assert again["run_id"] == "run-a" and again["status"] == "in_progress"
    monkeypatch.setattr(releases, "rev", lambda ref: "a" * 40)       # prod == previous tag
    monkeypatch.setattr(workspace, "fresh_worktree", lambda ref: pytest.fail("must not touch git"))
    v = promote.run(state(), FakeWatcher())
    assert v.status == "fail" and v.checks["claimed"] is False and "never run twice" in v.reasons[0]


def test_promote_without_baseline_traffic_is_unsure():
    empty = {"metrics": {}, "instance": "stable", "path": "*", "window_s": 10, "samples": 0}
    v = promote.run(state(), FakeWatcher({"stable": empty}))
    assert v.status == "unsure" and v.checks["baseline_has_samples"] is False


def test_files_touched():
    assert promote.files_touched(CART_FIX) == ["app/cart.py"]
    assert promote.files_touched(ORDERS_FIX) == ["app/orders.py"]


# ---------------------------------------------------------------- stub watcher

def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


TB = ('Traceback (most recent call last):\n'
      '  File "/x/site-packages/starlette/routing.py", line 1, in app\n'
      '  File "/tmp/sre-wt-1/target_service/app/reports.py", line 17, in daily_report\n'
      '  File "/tmp/sre-wt-1/target_service/app/orders.py", line 42, in order_total\n'
      '  File "/tmp/sre-wt-1/target_service/app/orders.py", line 38, in get_order\n'
      'app.orders.OrderNotFound: o-0998\n')


def write_log(path: Path, now: float) -> None:
    lines = []
    for i in range(20):                                   # stable, previous window: all 200, 10 ms
        lines.append({"ts": iso(now - 19 + i * 0.4), "instance": "stable", "path": "/cart/total",
                      "route": "/cart/total", "status": 200, "latency_ms": 10.0})
    for i in range(9):                                    # stable, current window
        lines.append({"ts": iso(now - 9 + i), "instance": "stable", "path": "/orders/o-1",
                      "route": "/orders/{order_id}", "status": 200, "latency_ms": float(i + 1)})
    lines.append({"ts": iso(now - 0.5), "instance": "stable", "path": "/reports/daily", "route": "/reports/daily",
                  "status": 500, "latency_ms": 100.0, "exception_type": "OrderNotFound", "traceback": TB})
    lines.append({"ts": iso(now - 1), "instance": "canary", "path": "/cart/alice", "route": "/cart/{user}",
                  "status": 200, "latency_ms": 3.0})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")


def test_stub_watcher_on_synthetic_log(tmp_path):
    now = time.time()
    log = tmp_path / "server.log"
    write_log(log, now)
    w = StubWatcher(log, clock=lambda: now)

    cur = w.current("*", "stable")
    assert cur["samples"] == 10 and cur["instance"] == "stable" and cur["path"] == "*"
    assert cur["metrics"]["error_rate_percent"] == 10.0 and cur["metrics"]["p95_latency_ms"] == 100.0
    assert w.current("/orders/{order_id}", "stable")["metrics"] == {"error_rate_percent": 0.0, "p95_latency_ms": 9.0}
    assert w.current("/reports/daily", "stable")["samples"] == 1

    base = w.baseline("*", "stable")
    assert base["samples"] == 20 and base["metrics"]["error_rate_percent"] == 0.0
    assert w.baseline("*", "stable", until=now - 30)["samples"] == 0      # no evidence => caller: unsure

    assert w.current("*", "canary")["samples"] == 1
    assert StubWatcher(tmp_path / "missing.log").current("*")["samples"] == 0

    assert w.signatures(now - 60, "stable") == Counter({"OrderNotFound@app/orders.py:get_order": 1})
    assert w.signatures(now - 60, "canary") == Counter()
    assert w.signatures(now - 60, "stable", until_ts=now - 1) == Counter()
    assert signature_of({"exception_type": "KeyError", "traceback": ""}) == "KeyError@?"


# ---------------------------------------------------------------- SLO judgement

def snap(err: float, p95: float, samples: int = 100) -> dict:
    return {"metrics": {"error_rate_percent": err, "p95_latency_ms": p95}, "instance": "canary",
            "path": "*", "window_s": 10, "samples": samples}


def test_judge_slo():
    assert canary.judge_slo(snap(0, 2.0), snap(0, 3.5))["ok"]             # latency noise below the floor
    bad = canary.judge_slo(snap(0, 2.0), snap(0.5, 2.0))                    # one stray 500 from a 0% baseline
    assert not bad["ok"] and bad["outcome"] == "regressed" and bad["checks"]["error_rate_within_slo"]
    assert canary.judge_slo(snap(14.0, 2.0), snap(0.5, 2.0))["outcome"] == "improved"
    assert not canary.judge_slo(snap(14.0, 2.0), snap(2.0, 2.0))["checks"]["error_rate_within_slo"]
    assert not canary.judge_slo(snap(0, 2.0), snap(0, 900.0))["ok"]
    assert canary.sample_offsets(12, 2, 10) == [10, 12.0]
    assert canary.sample_offsets(45, 2, 2)[:2] == [2, 4] and canary.sample_offsets(45, 2, 2)[-1] == 45.0


# ---------------------------------------------------------------- watch

def watching_release(baseline: dict) -> Release:
    r = rel("release-1", status="watching", promoted_ago_s=1)
    r = Release(**{**asdict(r), "files_touched": ["app/orders.py"], "baseline": baseline})
    releases.append(r)
    return r


def test_watch_clean_is_kept():
    r = watching_release(snap(0, 2.0))
    now = time.time()
    w = FakeWatcher(sigs=[(now - 20, "KeyError@app/cart.py:cart_total")])   # only before the release
    v = promote.watch(r, w, bug_signature="KeyError@app/cart.py:cart_total", window_s=6, interval_s=2,
                      judge_after_s=2, since_ts=now, sleep=lambda s: None)
    assert v.stage == "watch" and v.status == "pass" and v.route is None, v.reasons
    assert releases.get("release-1").status == "kept"


def test_watch_new_signature_in_touched_file_hands_to_rollback():
    r = watching_release(snap(14.0, 2.0))
    now = time.time()
    w = FakeWatcher(sigs=[(now - 20, "AttributeError@app/orders.py:read_order"),
                          (now + 0.5, "OrderNotFound@app/orders.py:get_order")])
    v = promote.watch(r, w, bug_signature="AttributeError@app/orders.py:read_order", window_s=6, interval_s=2,
                      judge_after_s=2, since_ts=now, sleep=time.sleep)
    assert v.status == "fail" and v.route == "rollback"
    assert v.checks["no_new_signatures_in_touched_files"] is False and v.checks["bug_signature_absent"] is True
    assert "OrderNotFound@app/orders.py:get_order" in " ".join(v.reasons)
    assert releases.get("release-1").status == "watching"


def test_watch_bug_signature_returning_fails():
    r = watching_release(snap(0, 2.0))
    now = time.time()
    w = FakeWatcher(sigs=[(now + 0.1, "KeyError@app/cart.py:cart_total")])
    v = promote.watch(r, w, bug_signature="KeyError@app/cart.py:cart_total", window_s=4, interval_s=1,
                      since_ts=now, sleep=time.sleep)
    assert v.status == "fail" and v.route == "rollback" and v.checks["bug_signature_absent"] is False


def test_watch_without_traffic_is_unsure():
    r = watching_release(snap(0, 2.0))
    empty = {"metrics": {}, "instance": "stable", "path": "*", "window_s": 10, "samples": 0}
    v = promote.watch(r, FakeWatcher({"stable": empty}), window_s=4, interval_s=2, judge_after_s=2,
                      since_ts=time.time(), sleep=lambda s: None)
    assert v.status == "unsure" and v.route == "rollback"
    assert releases.get("release-1").status == "watching"
