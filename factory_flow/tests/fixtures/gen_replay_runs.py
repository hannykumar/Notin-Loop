"""Build a realistic runs/ directory (audit.jsonl + artifacts/) for the B7 replay tests.

Two runs, laid out as documented in factory/replay.py:
  run_cart_promoted   BUG-CART, cart_fix.diff + fallback_tests/test_repro_cart.py:
                      inspect_diff pass, oracle pass, canary pass, promote pass.
  run_cart_rejected   BUG-CART, rogue_hardcode.diff: inspect_diff fail, escalate.

The inspect_diff and oracle verdicts are produced by actually running the stages (so recorded == replayed);
the traceback in bug.json is the real one from calling app/cart.py cart_total on a fresh worktree of the ref.
Earlier stages (alert .. fix) and canary/promote are recorded as a runner would, not executed.
Chain conventions follow gen_sample_audit.py (its Chain is reused; HMAC key TEST_HMAC_KEY).

Run from factory_flow/:  python tests/fixtures/gen_replay_runs.py <out_dir>
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

from gen_sample_audit import TEST_HMAC_KEY, Chain  # noqa: E402

from factory import workspace  # noqa: E402
from factory.contracts import BugReport, Verdict  # noqa: E402
from factory.replay import BUG_FILE, DEFAULT_REF, DIFF_FILE, rerun_inspect, sha256_file  # noqa: E402
from factory.sre_base import canonical_hash  # noqa: E402
from factory.stages import oracle  # noqa: E402

TARGET = workspace.TARGET_DIR
CART_FIX = TARGET / "patches" / "cart_fix.diff"
ROGUE = HERE / "rogue_hardcode.diff"
REPRO_CART = TARGET / "fallback_tests" / "test_repro_cart.py"
PROMOTED, REJECTED = "run_cart_promoted", "run_cart_rejected"

__all__ = ["TEST_HMAC_KEY", "PROMOTED", "REJECTED", "build"]


# STUB(track A): audit.append (A2) -- until factory/audit.py exists, entries are chained with gen_sample_audit.Chain
# (entry_hash = canonical_hash(entry without entry_hash/hmac), prev_hash, HMAC-SHA256(TEST_HMAC_KEY, entry_hash)).
def append_verdict(chain: Chain, run_id: str, v: Verdict, extra_evidence: dict | None = None) -> dict:
    return chain.add(run_id, v.stage, v.status, reasons=v.reasons, checks=v.checks,
                     evidence={**v.evidence, **(extra_evidence or {})}, route=v.route)


def real_cart_traceback(ref: str) -> str:
    """The real KeyError traceback from cart_total for a user with no cart, at `ref`."""
    wt = workspace.fresh_worktree(ref)
    try:
        proc = subprocess.run([sys.executable, "-c", "from app.cart import cart_total; cart_total('dave')"],
                              cwd=wt, capture_output=True, text=True, timeout=60)
    finally:
        workspace.remove_worktree(wt)
    tb = proc.stderr
    assert "KeyError" in tb and "cart_total" in tb, tb
    return tb


def _run_prefix(chain: Chain, run_id: str, art: Path, bug: BugReport, diff: str, fixer: str) -> dict:
    """alert .. fix, writing bug.json, the frozen test and fix.diff the way the runner would."""
    art.mkdir(parents=True, exist_ok=True)
    (art / BUG_FILE).write_text(json.dumps(asdict(bug), indent=2, sort_keys=True))
    test = art / REPRO_CART.name
    test.write_bytes(REPRO_CART.read_bytes())
    (art / DIFF_FILE).write_text(diff)
    hashes = {"bug": canonical_hash(asdict(bug)), "test": sha256_file(test), "diff": oracle.diff_hash(diff)}

    chain.start(run_id, "ALERT-cart-total", "/cart/total")
    chain.add(run_id, "triage", "pass", checks={"no_own_recent_release": True})
    chain.add(run_id, "investigate", "pass", step_s=15, checks={"evidence_gate": True, "replay_3_of_3": True},
              evidence={"bug_id": bug.id, "signature": bug.signature, "error_share": bug.error_share,
                        "replay_failures": bug.replay_failures, "bug_report_sha256": hashes["bug"],
                        "bug_path": f"runs/artifacts/{run_id}/{BUG_FILE}"})
    chain.add(run_id, "qualify", "pass", checks={"allowed_bug_class": True, "max_files": True,
                                                 "no_protected_paths": True, "no_deny_keywords": True,
                                                 "suite_green_on_prod": True})
    chain.add(run_id, "reproduce", "pass", step_s=20, checks={"fails_3_of_3": True, "right_reason": True,
                                                              "frozen": True},
              evidence={"test_path": test.name, "test_sha256": hashes["test"]})
    chain.add(run_id, "fix", "pass", step_s=30, checks={"valid_diff": True, "within_time_limit": True},
              evidence={"fixer": fixer, "diff_sha256": hashes["diff"],
                        "diff_path": f"runs/artifacts/{run_id}/{DIFF_FILE}"})
    return {"test_file": test, **hashes}


def build(out_dir: Path | str, ref: str = DEFAULT_REF) -> dict:
    """Write out_dir/audit.jsonl and out_dir/artifacts/<run_id>/...; return paths and the recorded verdicts."""
    out_dir = Path(out_dir)
    artifacts = out_dir / "artifacts"
    traceback = real_cart_traceback(ref)
    bug = BugReport(id="BUG-CART", alert_id="ALERT-cart-total", signature="KeyError@app/cart.py:cart_total",
                    exception_type="KeyError", traceback=traceback,
                    sample_request={"method": "GET", "path": "/cart/total", "query": {"user": "dave"}, "body": None},
                    files=["app/cart.py"], error_share=0.94, replay_failures=3,
                    evidence_ids=["alert:ALERT-cart-total", "logs:server.log", "replay:/cart/total"])
    chain = Chain()
    verdicts: dict[str, dict[str, str]] = {}

    # (a) promoted: inspect pass, oracle pass, canary pass, promote pass.
    diff = CART_FIX.read_text()
    art = artifacts / PROMOTED
    h = _run_prefix(chain, PROMOTED, art, bug, diff, "ScriptedFixer")
    test_text = h["test_file"].read_text()
    insp = rerun_inspect(diff, asdict(bug), test_text, ref)
    append_verdict(chain, PROMOTED, insp, {"test_sha256": h["test"], "ref": ref})
    orc = oracle.check(diff, h["test_file"], ref=ref, artifacts_dir=art)
    append_verdict(chain, PROMOTED, orc)
    chain.add(PROMOTED, "canary", "pass", step_s=14, checks={"diff_hash_matches": True, "slo_holds_vs_stable": True},
              evidence={"diff_sha256": h["diff"],
                        "canary": {"error_rate_percent": 0.0, "p95_latency_ms": 41.0, "samples": 58},
                        "stable": {"error_rate_percent": 31.0, "p95_latency_ms": 44.0, "samples": 236}})
    chain.add(PROMOTED, "promote", "pass", checks={"diff_hash_matches": True, "hash_not_already_released": True},
              evidence={"diff_sha256": h["diff"], "tag": "release-1", "commit": "a1c3e5f", "previous_tag": ref})
    verdicts[PROMOTED] = {"inspect_diff": insp.status, "oracle": orc.status}

    # (b) rejected at inspect_diff: no oracle entry, escalated.
    diff = ROGUE.read_text()
    art = artifacts / REJECTED
    h = _run_prefix(chain, REJECTED, art, bug, diff, "LLMFixer")
    insp = rerun_inspect(diff, asdict(bug), h["test_file"].read_text(), ref)
    append_verdict(chain, REJECTED, insp, {"test_sha256": h["test"], "ref": ref})
    chain.add(REJECTED, "escalate", insp.status, reasons=[f"inspect_diff: {insp.status}"],
              evidence={"inbox_case": f"runs/inbox/{REJECTED}.json", "severity": "normal"})
    verdicts[REJECTED] = {"inspect_diff": insp.status}

    audit = out_dir / "audit.jsonl"
    audit.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in chain.entries))
    return {"audit": audit, "artifacts": artifacts, "entries": chain.entries, "verdicts": verdicts}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "runs_replay_demo")
    try:
        built = build(out)
    finally:
        workspace.cleanup()
    print(f"wrote {len(built['entries'])} audit entries to {built['audit']}; artifacts in {built['artifacts']}")
    print(f"recorded verdicts: {built['verdicts']}  (HMAC key: {TEST_HMAC_KEY!r})")


if __name__ == "__main__":
    main()
