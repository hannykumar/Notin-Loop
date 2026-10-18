"""B6 unit checks: rollback method choice, git revert keeping newer releases, conflict, scope, health,
ledger/inbox/PAUSED. Real git in a temp copy of target_service; deploy_stable, sleep, probes and the watcher
are faked. The live end-to-end validation is tests/live_b6.py.
"""
import json
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from factory import releases, stub_inbox, workspace
from factory.contracts import BugReport, Release, RunState
from factory.stages import rollback as rb

REAL_TARGET = workspace.TARGET_DIR
CART_FIX = (REAL_TARGET / "patches" / "cart_fix.diff").read_text()
ORDERS_FIX = (REAL_TARGET / "patches" / "orders_helper_fix.diff").read_text()
# On top of cart_fix, changes the line cart_fix changed: reverting cart_fix underneath it conflicts.
_FIXED = 'cart.get("items", [])'
CART_CONFLICT = "\n".join(
    "-" + ln[1:].replace('cart["items"]', _FIXED) if ln.startswith("-    total")
    else "+" + ln[1:].replace(_FIXED, 'cart.get("items") or []') if ln.startswith("+    total")
    else ln
    for ln in CART_FIX.split("\n"))
BUGGY_CART = 'for item in cart["items"])'
BASELINE = {"metrics": {"error_rate_percent": 0.0, "p95_latency_ms": 4.0}, "instance": "stable", "path": "*",
            "window_s": 10.0, "samples": 200}
HEALTHY = {"metrics": {"error_rate_percent": 0.0, "p95_latency_ms": 4.0}, "instance": "stable", "path": "*",
           "window_s": 10.0, "samples": 180}
ORDERS_SIG = "OrderNotFound@app/orders.py:get_order"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp target_service repo (release-0 on main/prod), temp ledger/inbox/PAUSED, fake deploy."""
    target = tmp_path / "target_service"
    shutil.copytree(REAL_TARGET, target, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    git("init", "-q", "-b", "main", cwd=target)
    git("add", "-A", cwd=target)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "release-0", cwd=target)
    git("branch", "prod", cwd=target)
    git("tag", "release-0", cwd=target)
    monkeypatch.setattr(workspace, "TARGET_DIR", target)
    monkeypatch.setattr(releases, "LEDGER_PATH", tmp_path / "releases.json")
    monkeypatch.setattr(rb, "PAUSED_PATH", tmp_path / "PAUSED")
    monkeypatch.setattr(stub_inbox, "INBOX_DIR", tmp_path / "inbox")
    deployed: list[str] = []

    def fake_deploy(ref: str) -> dict:
        deployed.append(ref)
        return {"ref": ref, "commit": releases.rev(ref), "pid": 1, "port": 8101, "started_ts": time.time()}

    monkeypatch.setattr(releases, "deploy_stable", fake_deploy)
    return {"target": target, "tmp": tmp_path, "deployed": deployed}


def promote(diff: str, status: str = "watching", promoted_ago_s: float = 5.0) -> Release:
    """What promote.run does to git + ledger, without canary/stable."""
    prev = releases.previous_tag()
    old = releases.rev("prod")
    tag = releases.next_tag(existing_tags=releases.git_tags())
    wt = workspace.fresh_worktree(old)
    try:
        assert workspace.apply_diff(wt, diff)["applied"]
        releases.git("add", "-A", cwd=wt)
        releases.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", tag, cwd=wt)
        commit = releases.git("rev-parse", "HEAD", cwd=wt)
    finally:
        workspace.remove_worktree(wt)
    releases.git("tag", tag, commit)
    releases.git("update-ref", "refs/heads/prod", commit, old)
    ts = time.time() - promoted_ago_s
    files = ["app/cart.py"] if "app/cart.py" in diff else ["app/orders.py"]
    return releases.append(Release(tag=tag, commit=commit, previous_tag=prev, diff_sha256=f"sha-{tag}",
                                   run_id=f"run-{tag}", promoted_at=releases.utc_iso(ts),
                                   watch_until=releases.utc_iso(ts + 45), status=status, files_touched=files,
                                   baseline=dict(BASELINE)))


class FakeWatcher:
    def __init__(self, current=None, per_path=None, sigs=None, entries=()):
        self._current = current or HEALTHY
        self.per_path = per_path or {}
        self.sigs = sigs or (lambda since, until: Counter())
        self._entries = list(entries)

    def current(self, path, instance="stable"):
        return self.per_path.get(path, self._current)

    def baseline(self, path, instance="stable"):
        return BASELINE

    def signatures(self, since_ts, instance="stable", until_ts=None):
        return self.sigs(since_ts, until_ts if until_ts is not None else time.time())

    def entries(self, since_ts, until_ts, instance=None):
        return iter(self._entries)


def show_at(ref: str, path: str) -> str:
    return releases.git("show", f"{ref}:{path}")


def cases(env) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((env["tmp"] / "inbox").glob("*.json"))]


def call(tag: str, **kw):
    kw.setdefault("watcher", FakeWatcher())
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("probe", lambda path, query=None: 200)
    kw.setdefault("new_signatures", [])
    return rb.rollback(tag, **kw)


# ---------------------------------------------------------------- method choice

def test_latest_release_checks_out_previous_tag(env):
    r1 = promote(CART_FIX)
    audit: list[dict] = []
    entries = [{"method": "GET", "path": "/cart/total", "query": {"user": "dave"}, "status": 500,
                "exception_type": "KeyError", "traceback": 'File "/x/app/cart.py", line 3, in x'}]
    probed: list = []
    v = call("release-1", new_signatures=["KeyError@app/cart.py:x"], audit_append=audit.append,
             watcher=FakeWatcher(entries=entries), probe=lambda path, query=None: probed.append((path, query)) or 200)
    assert probed == [("/cart/total", {"user": "dave"})] * rb.PROBE_COUNT
    assert v.stage == "rollback" and v.status == "pass", v.reasons
    assert v.evidence["method"] == "checkout_previous_tag"
    assert v.evidence["prod_before"] == r1.commit
    assert v.evidence["prod_after"] == releases.rev("release-0") == releases.rev("prod")
    assert env["deployed"] == ["release-0"] and v.evidence["deployed_ref"] == "release-0"
    assert releases.get("release-1").status == "rolled_back"
    assert v.evidence["signatures_checked"] == ["KeyError@app/cart.py:x"] and v.checks["new_signatures_gone"]
    assert v.evidence["verification_outcome"] in ("unchanged", "improved") and v.evidence["actor"] == "agent"
    # inbox case reopens the bug with the failed fix; PAUSED after an agent rollback
    [case] = cases(env)
    assert Path(v.evidence["inbox_case"]).exists() and case["kind"] == "rollback" and case["severity"] == "high"
    assert case["evidence"]["failed_fix"]["tag"] == "release-1"
    assert case["evidence"]["failed_fix"]["diff_sha256"] == "sha-release-1"
    paused = json.loads((env["tmp"] / "PAUSED").read_text())
    assert paused["release"] == "release-1" and paused["reason"] and paused["ts"]
    assert audit and audit[0]["stage"] == "rollback" and audit[0]["actor"] == "agent" and audit[0]["status"] == "pass"
    assert releases.previous_tag() == "release-0"  # promote's ledger/prod invariant still holds


def test_older_release_git_revert_keeps_newer_release(env):
    r1 = promote(CART_FIX, status="kept")
    r2 = promote(ORDERS_FIX, status="watching")
    assert BUGGY_CART not in show_at("prod", "app/cart.py")
    v = call("release-1", actor="human")
    assert v.status == "pass", v.reasons
    assert v.evidence["method"] == "git_revert" and v.evidence["actor"] == "human"
    assert v.evidence["run_id"] == "manual-rollback-release-1"
    assert v.evidence["prod_before"] == r2.commit
    assert v.evidence["revert_tag"] == v.evidence["deployed_ref"] == "release-3" == env["deployed"][-1]
    prod = releases.rev("prod")
    assert prod == releases.rev("release-3") == v.evidence["prod_after"]
    assert BUGGY_CART in show_at("prod", "app/cart.py")          # release-1 reverted
    assert "class OrderNotFound" in show_at("prod", "app/orders.py")  # release-2 kept
    assert releases.git("rev-parse", "prod~1") == r2.commit
    ledger = {r.tag: r for r in releases.load()}
    assert ledger["release-1"].status == "rolled_back" and ledger["release-2"].status == "watching"
    assert ledger["release-3"].status == "kept" and ledger["release-3"].previous_tag == "release-2"
    assert ledger["release-3"].files_touched == ["app/cart.py"] and ledger["release-3"].commit == prod
    assert releases.previous_tag() == "release-3"
    assert not (env["tmp"] / "PAUSED").exists()                  # human rollback does not pause the line
    assert cases(env)[0]["evidence"]["rollback"]["actor"] == "human"
    assert workspace.list_worktrees() == []
    assert r1.commit != prod


def test_revert_conflict_changes_nothing_and_is_critical(env):
    promote(CART_FIX, status="kept")
    r2 = promote(CART_CONFLICT, status="watching")
    tags_before = releases.git_tags()
    ledger_before = releases.load()
    v = call("release-1")
    assert v.status == "fail" and v.evidence["method"] == "git_revert"
    assert v.checks["revert_clean"] is False and v.evidence["severity"] == "critical"
    assert v.evidence["conflicts"] == ["app/cart.py"]
    assert releases.rev("prod") == r2.commit and releases.git_tags() == tags_before
    assert releases.load() == ledger_before and env["deployed"] == []
    assert workspace.list_worktrees() == []
    assert releases.git("status", "--porcelain") == ""
    [case] = cases(env)
    assert case["severity"] == "critical" and (env["tmp"] / "PAUSED").exists()


# ---------------------------------------------------------------- scope

def test_refuses_non_ledger_already_rolled_back_and_old_releases(env):
    promote(CART_FIX, status="kept", promoted_ago_s=3600)
    releases.git("tag", "release-7", "release-0")                # a tag the agent did not make
    v = call("release-7", actor="human")
    assert v.status == "fail" and v.checks == {"release_in_ledger": False} and "ledger" in v.reasons[0]
    prod = releases.rev("prod")
    v = call("release-1")                                       # agent: kept and outside the lookback
    assert v.status == "fail" and v.checks["agent_may_roll_back"] is False
    assert releases.rev("prod") == prod and env["deployed"] == [] and releases.get("release-1").status == "kept"
    assert cases(env) == [] and not (env["tmp"] / "PAUSED").exists()
    v = call("release-1", actor="human")                        # a human may
    assert v.status == "pass" and releases.rev("prod") == releases.rev("release-0")
    v = call("release-1", actor="human")
    assert v.status == "fail" and v.checks["not_already_rolled_back"] is False and env["deployed"] == ["release-0"]


# ---------------------------------------------------------------- health verification

def test_health_not_restored_pauses_line(env):
    promote(ORDERS_FIX)
    w = FakeWatcher(sigs=lambda since, until: Counter({ORDERS_SIG: 2}))
    v = call("release-1", watcher=w, new_signatures=[ORDERS_SIG])
    assert v.status == "fail" and v.checks["new_signatures_gone"] is False and v.checks["health_restored"] is False
    assert v.evidence["severity"] == "critical" and (env["tmp"] / "PAUSED").exists()
    assert releases.get("release-1").status == "rolled_back"    # code was rolled back; health was not
    assert cases(env)[0]["severity"] == "critical"
    assert any("pause line" in r for r in v.reasons)


def test_no_samples_is_unsure(env):
    promote(ORDERS_FIX)
    empty = {"metrics": {}, "instance": "stable", "path": "*", "window_s": 10.0, "samples": 0}
    v = call("release-1", watcher=FakeWatcher(current=empty))
    assert v.status == "unsure" and v.checks["stable_has_samples"] is False
    assert v.evidence["severity"] == "critical" and (env["tmp"] / "PAUSED").exists()


def test_demo3_shape_computed_signatures_probe_and_reopened_bug(env):
    """New signature computed from the watch period, probed via its safe GET; the reopened bug's 500s
    (present in the baseline) are subtracted before the SLO judgement."""
    r1 = promote(ORDERS_FIX)
    promoted = releases.parse_iso(r1.promoted_at)
    old_sig = "AttributeError@app/orders.py:read_order"

    def sigs(since, until):
        if until <= promoted:                                   # baseline period
            return Counter({old_sig: 30})
        if env["deployed"]:                                     # after the restart
            return Counter({old_sig: 25})
        return Counter({ORDERS_SIG: 1, old_sig: 4})            # watch period

    tb = 'Traceback:\n  File "/x/target_service/app/reports.py", line 1, in daily_report\n' \
         '  File "/x/target_service/app/orders.py", line 2, in get_order\nOrderNotFound\n'
    entries = [{"ts": "x", "instance": "stable", "method": "GET", "path": "/reports/daily", "query": {},
                "status": 500, "exception_type": "OrderNotFound", "traceback": tb}]
    current = {**HEALTHY, "metrics": {"error_rate_percent": 12.5, "p95_latency_ms": 4.0}, "samples": 200}
    bug_path = {**HEALTHY, "path": "/orders/o-9999", "metrics": {"error_rate_percent": 100.0, "p95_latency_ms": 3.0},
                "samples": 25}
    w = FakeWatcher(current=current, per_path={"/orders/o-9999": bug_path}, sigs=sigs, entries=entries)
    BASE_404 = {**BASELINE, "metrics": {"error_rate_percent": 0.0, "p95_latency_ms": 4.0}}
    releases.update("release-1", baseline=BASE_404)
    bug = BugReport(id="B", alert_id="A", signature=old_sig, exception_type="AttributeError", traceback="",
                    sample_request={"method": "GET", "path": "/orders/o-9999"}, files=["app/orders.py"],
                    error_share=1.0, replay_failures=3, evidence_ids=[])
    probed: list[str] = []
    v = rb.rollback("release-1", watcher=w, bug=bug, sleep=lambda s: None,
                    probe=lambda path, query=None: probed.append(path) or 200)
    assert v.evidence["new_signatures"] == [ORDERS_SIG]
    assert probed == ["/reports/daily"] * rb.PROBE_COUNT and v.checks["probes_ok"]
    assert v.evidence["verification"]["judgement"]["ok"] is False          # 12.5% incl. the reopened bug
    adj = v.evidence["verification"]["current_excluding_reopened_bug"]
    assert adj["samples"] == 175 and adj["metrics"]["error_rate_percent"] == 0.0
    assert v.status == "pass", v.reasons
    case = cases(env)[0]["evidence"]
    assert case["reopened_bug"]["signature"] == old_sig and case["failed_fix"]["new_signatures"] == [ORDERS_SIG]


def test_unprobeable_signature_is_unsure_and_failing_probe_fails(env):
    promote(ORDERS_FIX)
    v = call("release-1", new_signatures=[ORDERS_SIG])          # no entries -> nothing to probe
    assert v.status == "unsure" and v.checks["new_signature_paths_probed"] is False
    promote(ORDERS_FIX.replace("not found", "missing"))          # release-2 on release-0 again
    entries = [{"method": "GET", "path": "/reports/daily", "query": {}, "status": 500,
                "exception_type": "OrderNotFound",
                "traceback": 'File "/x/app/orders.py", line 2, in get_order'}]
    v = call("release-2", new_signatures=[ORDERS_SIG], watcher=FakeWatcher(entries=entries),
             probe=lambda path, query=None: 500)
    assert v.status == "fail" and v.checks["probes_ok"] is False


def test_run_wrapper_without_release():
    v = rb.run(RunState(run_id="r", alert={}, artifacts_dir=Path("/tmp")))
    assert v.stage == "rollback" and v.status == "unsure"
