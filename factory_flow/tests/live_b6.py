"""Live B6 validation with the real policy windows (not collected by pytest).

    cd factory_flow && python tests/live_b6.py

From a clean state (init_repo.sh --force, empty runs/), reusing live_b5's helpers:
  a. cart_fix.diff -> canary -> promote release-1 -> watch clean -> kept
  c. orders_helper_fix.diff under break_404 -> canary pass -> promote release-2 -> watch fail, route rollback
  d. agent rollback of release-2 (rollback.run(state)): checkout_previous_tag -> prod at release-1, stable
     restarted, /reports/daily probed 200, OrderNotFound gone, health verified, ledger rolled_back, inbox
     case + runs/PAUSED
  e. manual rollback of release-1 (`python -m factory.stages.rollback release-1`, actor human) under normal
     traffic -> prod and stable back at release-0
Exit code 0 only if every expectation holds. Processes are always stopped in finally.
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
sys.path.insert(0, str(FACTORY_FLOW / "tests"))

import httpx  # noqa: E402

from live_b5 import CONTROL, FAILURES, RUNS, TARGET, expect, make_state, show, wait_for_log  # noqa: E402

from factory import releases, workspace  # noqa: E402
from factory.contracts import Verdict  # noqa: E402
from factory.policy import CANARY_PORT, STABLE_PORT  # noqa: E402
from factory.stages import canary, promote, rollback  # noqa: E402

ORDERS_SIG = "OrderNotFound@app/orders.py:get_order"


def show_rb(v: Verdict) -> None:
    keys = ("method", "actor", "prod_before", "prod_after", "deployed_ref", "verification_outcome",
            "signatures_checked", "new_signatures_source", "ledger_status", "inbox_case", "paused", "severity")
    print(f"Verdict(stage={v.stage!r}, status={v.status!r}, route={v.route!r})")
    print(f"  reasons: {v.reasons}")
    print(f"  checks:  {v.checks}")
    print(f"  evidence: {json.dumps({k: v.evidence.get(k) for k in keys}, default=str)}")
    ver = v.evidence.get("verification") or {}
    print(f"  verification: probes={ver.get('probes')} signatures_after_restart={ver.get('signatures_after_restart')}")
    for k in ("current", "current_excluding_reopened_bug"):
        if k in ver:
            print(f"    {k}: metrics={ver[k].get('metrics')} samples={ver[k].get('samples')}")
    print(f"    baseline: metrics={(ver.get('baseline') or {}).get('metrics')}")


def stable_get(path: str) -> int:
    return httpx.get(f"http://127.0.0.1:{STABLE_PORT}{path}", timeout=5).status_code


def main() -> int:
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

        # ---------------------------------------------------------------- a (live_b5)
        print("\n== a. cart_fix.diff: canary -> promote -> watch")
        sa = make_state("run-a", TARGET / "patches" / "cart_fix.diff", "KeyError@app/cart.py:cart_total", "/cart/total")
        v = canary.run(sa)
        show(v)
        expect(v.status == "pass", "a: canary pass")
        v = promote.run(sa)
        show(v)
        expect(v.status == "pass" and v.evidence.get("tag") == "release-1", "a: promoted as release-1")
        v = promote.watch_stage(sa)
        show(v)
        expect(v.status == "pass", "a: watch clean, release-1 kept")
        release1_commit = releases.rev("release-1")

        # ---------------------------------------------------------------- c (live_b5)
        print("\n== c. orders_helper_fix.diff under break_404: canary -> promote -> watch -> rollback route")
        canary.set_traffic(profile="break_404")
        sc = make_state("run-c", TARGET / "patches" / "orders_helper_fix.diff",
                        "AttributeError@app/orders.py:read_order", "/orders/o-9999")
        time.sleep(12)
        hit = wait_for_log("/reports/daily", time.time(), 25)
        print(f"  /reports/daily hit at {hit}; starting canary")
        v = canary.run(sc)
        show(v)
        expect(v.status == "pass", "c: canary pass")
        v = promote.run(sc)
        show(v)
        expect(v.status == "pass" and v.evidence.get("tag") == "release-2", "c: promoted as release-2")
        watch_v = promote.watch_stage(sc)
        show(watch_v)
        expect(watch_v.status == "fail" and watch_v.route == "rollback", "c: watch fail, route rollback")
        expect(stable_get("/reports/daily") == 500, "c: /reports/daily is 500 on stable at release-2")

        # ---------------------------------------------------------------- d. agent rollback
        print("\n== d. agent rollback of release-2 (rollback.run(state), signatures computed from the watch period)")
        if watch_v.route == "rollback":
            v = rollback.run(sc)
            show_rb(v)
            expect(v.status == "pass", "d: rollback pass (health verified)")
            expect(v.evidence.get("method") == "checkout_previous_tag", "d: method checkout_previous_tag")
            expect(ORDERS_SIG in (v.evidence.get("signatures_checked") or []) and v.checks.get("new_signatures_gone") is True,
                   "d: OrderNotFound@app/orders.py:get_order checked and gone")
            probes = (v.evidence.get("verification") or {}).get("probes") or []
            expect(any(p["path"] == "/reports/daily" and all(s == 200 for s in p["statuses"]) for p in probes),
                   "d: /reports/daily probed: 200")
            expect(stable_get("/reports/daily") == 200, "d: /reports/daily returns 200 again on stable")
            expect(releases.get("release-2").status == "rolled_back", "d: ledger release-2 rolled_back")
            expect(releases.rev("prod") == release1_commit, "d: prod back at release-1 commit")
            expect(releases.stable_state().get("ref") == "release-1", "d: stable runs release-1")
            case = Path(v.evidence.get("inbox_case", "/nonexistent"))
            expect(case.exists() and json.loads(case.read_text())["evidence"]["failed_fix"]["tag"] == "release-2",
                   "d: inbox case with the failed fix")
            expect(rollback.PAUSED_PATH.exists(), "d: runs/PAUSED exists")
            print(f"  PAUSED: {rollback.PAUSED_PATH.read_text() if rollback.PAUSED_PATH.exists() else None}")

        # ---------------------------------------------------------------- e. manual rollback
        print("\n== e. manual rollback of release-1: python -m factory.stages.rollback release-1 (actor human)")
        canary.set_traffic(profile="normal")
        time.sleep(1)
        proc = subprocess.run([sys.executable, "-m", "factory.stages.rollback", "release-1"], cwd=str(FACTORY_FLOW),
                              capture_output=True, text=True, timeout=120)
        print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip()[-2000:])
        out = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
        expect(proc.returncode == 0 and out.get("status") == "pass", "e: manual rollback pass")
        expect(out.get("evidence", {}).get("actor") == "human", "e: actor human")
        expect(releases.rev("prod") == releases.rev("release-0"), "e: prod back at release-0")
        expect(releases.stable_state().get("ref") == "release-0", "e: stable runs release-0")
        expect(releases.get("release-1").status == "rolled_back", "e: ledger release-1 rolled_back")
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
        print(f"  {r.tag} {r.status} prev={r.previous_tag} commit={r.commit[:10]} files={r.files_touched}")
    print(f"== inbox: {sorted(p.name for p in (RUNS / 'inbox').glob('*.json'))}")
    print(f"\n{'ALL B6 EXPECTATIONS MET' if not FAILURES else 'FAILED: ' + '; '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
