"""Live B8 validation: render the report from REAL stage verdicts (not collected by pytest).

    cd factory_flow && python tests/live_b8.py

`run-all` is a T1 (together) deliverable, so this is the closest Track B can get on its own:
  1. real inspect_diff + oracle verdicts for demo 1 (cart_fix.diff) and demo 3 (orders_helper_fix.diff) on release-0,
     using real tracebacks captured from the running code;
  2. live_b6.main(): real canary, promote, watch and rollback against the live server (demo 1 kept, demo 3 rolled back);
  3. every real Verdict is chained into runs/audit.jsonl; then report.render_html() renders runs/report.html from
     that audit log and the real runs/releases.json, and the chain is verified.
Exit code 0 only if every expectation holds.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

FACTORY_FLOW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_FLOW))
sys.path.insert(0, str(FACTORY_FLOW / "tests"))
sys.path.insert(0, str(FACTORY_FLOW / "tests" / "fixtures"))

import live_b6  # noqa: E402
from gen_sample_audit import TEST_HMAC_KEY, Chain  # noqa: E402
from live_b5 import FAILURES, RUNS, TARGET, expect  # noqa: E402

from factory import report  # noqa: E402
from factory.contracts import Verdict  # noqa: E402
from factory.stages import inspect_diff, oracle  # noqa: E402

DEMOS = {
    "run-a": {"patch": "cart_fix.diff", "repro": "test_repro_cart.py", "path": "/cart/total",
              "request": ("/cart/total", {"user": "dave"})},
    "run-c": {"patch": "orders_helper_fix.diff", "repro": "test_repro_404.py", "path": "/orders/{order_id}",
              "request": ("/orders/o-9999", {})},
}


def real_traceback(path: str, query: dict) -> str:
    """Call the buggy endpoint on release-0 in-process and return the traceback the plugin logged."""
    log = RUNS / "traceback_probe.log"
    code = ("from fastapi.testclient import TestClient; from app.main import app; "
            f"TestClient(app).get({path!r}, params={query!r})")
    subprocess.run([sys.executable, "-c", code], cwd=str(TARGET), check=True,
                   env={**os.environ, "SRE_LOG_PATH": str(log), "PYTHONPATH": str(TARGET)})
    line = json.loads(log.read_text().splitlines()[-1])
    log.unlink()
    return line["traceback"]


def plain(verdict: Verdict) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(verdict), default=str))


def main() -> int:
    subprocess.run([str(TARGET / "scripts" / "init_repo.sh"), "--force"], check=True)
    RUNS.mkdir(parents=True, exist_ok=True)

    print("== 1. real inspect_diff + oracle on release-0")
    pre: dict[str, list[dict]] = {}
    diffs: dict[str, str] = {}
    for run_id, demo in DEMOS.items():
        diff = (TARGET / "patches" / demo["patch"]).read_text()
        repro = TARGET / "fallback_tests" / demo["repro"]
        diffs[run_id] = diff
        tb = real_traceback(*demo["request"])
        iv = inspect_diff.inspect(diff, traceback=tb, frozen_test=repro.read_text(), worktree=TARGET)
        ov = oracle.check(diff, repro, ref="release-0")
        print(f"  {run_id}: inspect_diff={iv.status} oracle={ov.status}")
        expect(iv.status == "pass" and ov.status == "pass", f"1: {run_id} inspect_diff and oracle pass")
        pre[run_id] = [plain(iv), plain(ov)]

    print("\n== 2. live_b6 (real canary / promote / watch / rollback), capturing every verdict")
    captured: list[dict] = []
    show, show_rb = live_b6.show, live_b6.show_rb
    live_b6.show = lambda v: (captured.append(plain(v)), show(v))[1]
    live_b6.show_rb = lambda v: (captured.append(plain(v)), show_rb(v))[1]
    try:
        live_code = live_b6.main()  # clears runs/ at start; audit is written afterwards
    finally:
        live_b6.show, live_b6.show_rb = show, show_rb
    expect(live_code == 0, "2: live_b6 expectations met")
    stages = [v["stage"] for v in captured]
    print(f"  captured: {stages}")
    expect(stages == ["canary", "promote", "watch", "canary", "promote", "watch", "rollback"],
           "2: captured canary/promote/watch for both demos + rollback for demo 3")
    live = {"run-a": captured[:3], "run-c": captured[3:]}

    print("\n== 3. chain real verdicts into runs/audit.jsonl and render the report")
    # STUB(track A): audit.append (A2) -- Chain builds the PRD 7.7 hash chain + HMAC.
    chain = Chain()
    for run_id, demo in DEMOS.items():
        chain.start(run_id, f"ALERT-{run_id}", demo["path"])
        chain.add(run_id, "fix", "pass", evidence={"fixer": "ScriptedFixer", "diff": diffs[run_id],
                                                    "diff_sha256": oracle.diff_hash(diffs[run_id])})
        for v in pre[run_id] + live[run_id]:
            chain.add(run_id, v["stage"], v["status"], v["reasons"], v["checks"], v["evidence"], route=v.get("route"))
        if live[run_id][-1]["stage"] == "rollback":
            ev = live[run_id][-1]["evidence"]
            chain.add(run_id, "escalate", "unsure", ["release rolled back; bug reopened; line paused"],
                      evidence={"inbox_case": ev.get("inbox_case"), "severity": ev.get("severity")})
    audit_path = RUNS / "audit.jsonl"
    audit_path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in chain.entries))

    out = report.render_html(audit_path, RUNS / "releases.json", RUNS / "report.html", hmac_key=TEST_HMAC_KEY)
    html = Path(out).read_text()
    ok, bad = report.verify_chain(report.load_audit(audit_path), hmac_key=TEST_HMAC_KEY)
    summary = report.summarize(report.load_audit(audit_path), report.load_releases(RUNS / "releases.json"))
    outcomes = {run["run_id"]: run["outcome"] for run in summary["runs"]}
    print(f"  report: {out}")
    print(f"  chain verified: {ok} (first bad seq {bad}); entries {len(chain.entries)}")
    print(f"  outcomes: {outcomes}")
    print(f"  scoreboard: {summary['scoreboard']}")
    expect(ok, "3: audit chain verifies")
    expect(outcomes.get("run-c") == "rolled_back", "3: demo 3 outcome rolled_back")
    expect("chain verified" in html and "run-a" in html and "run-c" in html, "3: HTML has badge and both runs")
    expect("cart.get(&#34;items&#34;, [])" in html or "cart.get(&quot;items&quot;, [])" in html, "3: real diff rendered")
    expect(summary["scoreboard"].get("bad_changes_left", 0) == 0, "3: bad changes left on the server: 0")
    print(f"\n{'ALL B8 EXPECTATIONS MET' if not FAILURES else 'FAILED: ' + '; '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
