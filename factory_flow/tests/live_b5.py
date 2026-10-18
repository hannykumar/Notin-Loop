"""Live B5 validation with the real policy windows (not collected by pytest).

    cd factory_flow && python tests/live_b5.py

From a clean state (init_repo.sh --force, empty runs/): stable at release-0 on :8101 + traffic generator, then
(b runs first: the fixture also contains the cart_total fix, so it only applies to release-0)
  a. cart_fix.diff               -> canary pass -> promote release-1 -> watch clean -> ledger "kept"
  b. canary_breaks_endpoint.diff -> canary fail (canary killed, stable untouched, no release)
  c. orders_helper_fix.diff      -> (traffic break_404) canary pass -> promote release-2 -> watch sees
                                    OrderNotFound@app/orders.py:get_order (from /reports/daily) -> fail, route rollback
Exit code 0 only if every expectation holds.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

FACTORY_FLOW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_FLOW))

from factory import releases, workspace  # noqa: E402
from factory.contracts import BugReport, RunState, Verdict  # noqa: E402
from factory.policy import CANARY_PORT, STABLE_PORT  # noqa: E402
from factory.sre_base import canonical_hash  # noqa: E402
from factory.stages import canary, promote  # noqa: E402
from factory.releases import parse_iso as parse_ts  # noqa: E402

TARGET = workspace.target_dir()
RUNS = workspace.runs_dir()
CONTROL = RUNS / "traffic_control.json"
FAILURES: list[str] = []


def expect(cond: bool, what: str) -> None:
    print(f"  [{'ok' if cond else 'FAILED'}] {what}")
    if not cond:
        FAILURES.append(what)


def show(v: Verdict) -> None:
    ev = {k: v.evidence[k] for k in ("tag", "commit", "previous_tag", "files_touched", "diff_sha256",
                                     "new_signatures_elsewhere", "ledger_status") if k in v.evidence}
    if "samples" in v.evidence:
        last = v.evidence["samples"][-1]
        ev["last_sample"] = {k: last[k] for k in ("offset_s", "signatures") if k in last}
        for inst in ("stable", "canary", "current"):
            if inst in last:
                ev["last_sample"][inst] = {"metrics": last[inst]["metrics"], "samples": last[inst]["samples"]}
        if "judgement" in last:
            ev["last_sample"]["outcome"] = last["judgement"]["outcome"]
    print(f"Verdict(stage={v.stage!r}, status={v.status!r}, route={v.route!r})")
    print(f"  reasons: {v.reasons}")
    print(f"  checks:  {v.checks}")
    print(f"  evidence: {json.dumps(ev, default=str)}")


def make_state(run_id: str, diff_path: Path, signature: str, path: str) -> RunState:
    diff = diff_path.read_text()
    exc, where = signature.split("@", 1)
    bug = BugReport(id=f"BUG-{run_id}", alert_id=f"ALERT-{run_id}", signature=signature, exception_type=exc,
                    traceback=f'Traceback (most recent call last):\n  File "/srv/target_service/{where.split(":")[0]}", '
                              f'line 1, in {where.split(":")[1]}\n{exc}\n',
                    sample_request={"method": "GET", "path": path, "query": {}, "body": None},
                    files=[where.split(":")[0]], error_share=1.0, replay_failures=3, evidence_ids=["E000", "E001"])
    art = RUNS / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    return RunState(run_id=run_id, alert={"incident_id": f"ALERT-{run_id}"}, artifacts_dir=art, bug=bug,
                    diff=diff, diff_sha256=canonical_hash(diff))


def wait_for_log(path: str, after_ts: float, timeout_s: float) -> float | None:
    """Wait for a stable log line for `path` newer than after_ts; return its ts."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            for line in (RUNS / "server.log").read_text().splitlines()[-400:]:
                e = json.loads(line)
                if e.get("path") == path and e.get("instance") == "stable" and parse_ts(e["ts"]) > after_ts:
                    return parse_ts(e["ts"])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    return None


def main() -> int:
    # ---------------------------------------------------------------- clean state
    subprocess.run([str(TARGET / "scripts" / "init_repo.sh"), "--force"], check=True)
    if RUNS.exists():
        shutil.rmtree(RUNS)
    RUNS.mkdir(parents=True)
    os.environ["TRAFFIC_CONTROL"] = str(CONTROL)
    for port in (STABLE_PORT, CANARY_PORT):
        if workspace.port_busy(port):
            print(f"port {port} busy; aborting")
            return 2
    canary.set_traffic(profile="normal", canary_url="", canary_share=0.2)

    gen = None
    gen_out = open(RUNS / "traffic.out", "w")
    try:
        print("== stable at release-0 + traffic generator")
        print(" ", releases.deploy_stable("release-0"))
        gen = subprocess.Popen([sys.executable, "-m", "traffic.generate", "--stable", f"http://127.0.0.1:{STABLE_PORT}"],
                               cwd=str(TARGET), env={**os.environ, "TRAFFIC_CONTROL": str(CONTROL)},
                               stdout=gen_out, stderr=subprocess.STDOUT)
        time.sleep(12)

        # ---------------------------------------------------------------- b
        print("\n== b. canary_breaks_endpoint.diff: canary must fail")
        sb = make_state("run-b", FACTORY_FLOW / "tests" / "fixtures" / "canary_breaks_endpoint.diff",
                        "KeyError@app/cart.py:cart_total", "/cart/total")
        stable_before = releases.stable_state()
        v = canary.run(sb)
        show(v)
        expect(v.status == "fail", "b: canary fail")
        expect(not workspace.port_busy(CANARY_PORT) and "canary" not in workspace.instances(), "b: canary killed")
        expect(releases.stable_state() == stable_before and workspace.port_busy(STABLE_PORT), "b: stable untouched")
        expect(v.checks.get("diff_applies") is True and v.checks.get("canary_not_regressed") is False,
               "b: diff applied; failed on the SLO comparison (not on apply)")
        expect(releases.load() == [] and releases.rev("prod") == releases.rev("release-0"), "b: no release")
        expect(json.loads(CONTROL.read_text()).get("canary_url") == "", "b: traffic canary_url reset")

        # ---------------------------------------------------------------- a
        print("\n== a. cart_fix.diff: canary -> promote -> watch")
        sa = make_state("run-a", TARGET / "patches" / "cart_fix.diff", "KeyError@app/cart.py:cart_total", "/cart/total")
        v = canary.run(sa)
        show(v)
        expect(v.status == "pass", "a: canary pass")
        if v.status == "pass":
            v = promote.run(sa)
            show(v)
            expect(v.status == "pass" and v.evidence.get("tag") == "release-1", "a: promoted as release-1")
            v = promote.watch_stage(sa)
            show(v)
            expect(v.status == "pass", "a: watch clean")
        expect(releases.get("release-1") is not None and releases.get("release-1").status == "kept",
               "a: ledger release-1 kept")
        expect(releases.rev("prod") == releases.rev("release-1"), "a: prod branch points at release-1")
        expect(releases.stable_state().get("ref") == "release-1", "a: stable runs release-1")

        # ---------------------------------------------------------------- c
        print("\n== c. orders_helper_fix.diff under break_404: canary -> promote -> watch -> rollback route")
        canary.set_traffic(profile="break_404")
        sc = make_state("run-c", TARGET / "patches" / "orders_helper_fix.diff",
                        "AttributeError@app/orders.py:read_order", "/orders/o-9999")
        time.sleep(12)  # the bug exists: stable serves 500s for /orders/o-9999
        # Start right after a /reports/daily hit so the 12 s canary falls between two of them (as in demo 3).
        hit = wait_for_log("/reports/daily", time.time(), 25)
        print(f"  /reports/daily hit at {hit}; starting canary")
        v = canary.run(sc)
        show(v)
        expect(v.status == "pass", "c: canary pass (short canary misses /reports/daily)")
        if v.status == "pass":
            v = promote.run(sc)
            show(v)
            expect(v.status == "pass" and v.evidence.get("tag") == "release-2", "c: promoted as release-2")
            v = promote.watch_stage(sc)
            show(v)
            expect(v.status == "fail" and v.route == "rollback", "c: watch fail, route rollback")
            expect(any("OrderNotFound@app/orders.py:get_order" in r for r in v.reasons),
                   "c: new signature OrderNotFound@app/orders.py:get_order in touched file")
        expect(releases.get("release-2") is not None and releases.get("release-2").status == "watching",
               "c: ledger release-2 still watching (rollback is B6)")
    finally:
        try:
            canary.set_traffic(profile="normal", canary_url="")
        finally:
            if gen is not None:
                gen.terminate()
                try:
                    gen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    gen.kill()
            gen_out.close()
            workspace.cleanup()
            releases.stop_stable()

    print("\n== ledger runs/releases.json")
    for r in releases.load():
        print(f"  {r.tag} {r.status} prev={r.previous_tag} commit={r.commit[:10]} files={r.files_touched} "
              f"baseline={r.baseline.get('metrics')} samples={r.baseline.get('samples')}")
    print(f"\n{'ALL B5 EXPECTATIONS MET' if not FAILURES else 'FAILED: ' + '; '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
