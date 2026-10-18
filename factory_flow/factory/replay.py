"""Verify the audit chain and re-derive a run's decision (PRD B7).

replay(run_id):
  1. Verify the whole audit chain and every HMAC (audit.verify: seq 1..n, prev_hash chain, entry_hash, HMAC).
     Any break stops the replay: not reproduced, and the first bad seq is reported.
  2. Reload the run's stored artifacts (bug report, diff, frozen test), recompute their hashes and compare
     them with every hash recorded in the run's audit evidence. Any mismatch or missing artifact stops the
     replay: what would be re-run is not what was checked.
  3. Re-run the deterministic gates that the run actually reached (inspect_diff, then oracle; a run rejected
     at inspect_diff has no oracle entry) and compare each replayed status with the recorded one.
  4. Print "decision reproduced" or the mismatch lines.

# Patterned on SRE-agent/agent/approval.py @ b4f7e0c (verify_proposal_artifact: recompute canonical hashes of
# the stored artifact and refuse when what you are about to run differs from what was checked).
# Patterned on SRE-agent/evaluation/replays.py @ b4f7e0c (replay a recorded case through the same deterministic
# code and compare the outcome with the recorded expectation).
New code otherwise: SRE-agent has no hash-chained log replay.

Artifact layout (Track A's runner writes this at T1; replay reads only these files)
------------------------------------------------------------------------------------
runs/artifacts/<run_id>/
    bug.json          json.dumps(dataclasses.asdict(BugReport))            written by investigate
    <frozen test>     the frozen repro test file, e.g. test_repro_cart.py   written by reproduce
    fix.diff          the fixer's diff, exact text (no normalisation)       written by fix

Audit evidence keys replay relies on (PRD 7.7 entries, written through audit.append):
    investigate   evidence.bug_report_sha256 = canonical_hash(asdict(BugReport))   (== canonical_hash(json.load(bug.json)))
    reproduce     evidence.test_path         = frozen test path RELATIVE to runs/artifacts/<run_id>/
                  evidence.test_sha256       = canonical_hash(frozen test text)  (same as oracle's)
    fix           evidence.diff_sha256       = canonical_hash(diff)  (oracle.diff_hash)
    inspect_diff  status/checks/reasons      = the inspect_diff Verdict
                  evidence                   = Verdict.evidence (has diff_sha256) + test_sha256 + ref
    oracle        status/checks/reasons      = the oracle Verdict
                  evidence                   = Verdict.evidence (has diff_sha256, test_sha256, ref)
Every diff_sha256 / test_sha256 / bug_report_sha256 found anywhere in the run's evidence is checked.

Ref: the oracle must replay against the commit the run tested, not today's `prod` (prod moves after each
promotion). Replay uses evidence.ref of the oracle (or inspect_diff) entry; the runner should record the
tag/commit it tested (e.g. "release-0" or a sha). When no ref is recorded, replay assumes "release-0".

Key: HMACs are checked with AUDIT_HMAC_KEY (env or factory_flow/.env). Without a key replay refuses
(missing evidence is never a pass).

Usage:  python -m factory.replay <run_id> [--audit P] [--artifacts P] [--ref REF]
Exit code 0 if the decision is reproduced, else 1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from factory import audit, workspace
from factory.report import load_audit
from factory.sre_base import canonical_hash
from factory.stages import inspect_diff, oracle

DEFAULT_REF = "release-0"
BUG_FILE = "bug.json"
DIFF_FILE = "fix.diff"
REPLAYED_STAGES = ("inspect_diff", "oracle")
HASH_KEYS = ("bug_report_sha256", "test_sha256", "diff_sha256")


def test_hash(path: Path) -> str:
    """test_sha256 as reproduce and oracle record it."""
    return canonical_hash(Path(path).read_text())


def check_chain(audit_path: Path, entries: list[dict], hmac_key: str) -> tuple[bool, Optional[int], str]:
    if not entries:
        return False, None, "audit log is empty or missing"
    ok, bad = audit.verify(audit_path, hmac_key)
    return ok, bad, "" if ok else f"hash chain or HMAC broken at seq {bad}"


def _last(entries: list[dict], stage: str) -> Optional[dict]:
    found = [e for e in entries if e.get("stage") == stage]
    return found[-1] if found else None


def _load_artifacts(run_entries: list[dict], art_dir: Path) -> tuple[dict, list[str]]:
    """Reload bug.json, fix.diff and the frozen test; recompute hashes; compare with every recorded hash.
    Returns (artifacts, problems). Patterned on verify_proposal_artifact: refuse on any difference."""
    problems: list[str] = []
    arts: dict[str, Any] = {"bug": None, "diff": None, "test_path": None, "test_text": None, "hashes": {}}

    bug_path = art_dir / BUG_FILE
    if bug_path.is_file():
        try:
            arts["bug"] = json.loads(bug_path.read_text())
            arts["hashes"]["bug_report_sha256"] = canonical_hash(arts["bug"])
        except json.JSONDecodeError as exc:
            problems.append(f"{BUG_FILE} does not parse: {exc}")
    diff_path = art_dir / DIFF_FILE
    if diff_path.is_file():
        arts["diff"] = diff_path.read_text()
        arts["hashes"]["diff_sha256"] = oracle.diff_hash(arts["diff"])
    reproduce = _last(run_entries, "reproduce")
    rel = (reproduce or {}).get("evidence", {}).get("test_path")
    if rel:
        test_path = (art_dir / rel).resolve()
        if art_dir.resolve() not in test_path.parents:
            problems.append(f"reproduce test_path {rel!r} escapes the artifacts dir")
        elif test_path.is_file():
            arts["test_path"] = test_path
            arts["test_text"] = test_path.read_text()
            arts["hashes"]["test_sha256"] = test_hash(test_path)

    for entry in run_entries:
        evidence = entry.get("evidence") or {}
        for key in HASH_KEYS:
            if key not in evidence:
                continue
            actual = arts["hashes"].get(key)
            if actual is None:
                problems.append(f"seq {entry.get('seq')} {entry.get('stage')}: {key} recorded but artifact missing")
            elif actual != evidence[key]:
                problems.append(f"seq {entry.get('seq')} {entry.get('stage')}: {key} mismatch "
                                f"(recorded {str(evidence[key])[:12]}, artifact {actual[:12]})")
    return arts, problems


def _needed_problems(arts: dict, stages: list[str], run_entries: list[dict]) -> list[str]:
    """Each artifact a replayed gate consumes must exist and must have a recorded hash to compare with."""
    recorded = {k for e in run_entries for k in (e.get("evidence") or {}) if k in HASH_KEYS}
    needs = {"diff_sha256": ("fix.diff", arts["diff"])}
    needs["test_sha256"] = ("frozen test (reproduce evidence.test_path)", arts["test_path"])
    if "inspect_diff" in stages:
        needs["bug_report_sha256"] = (BUG_FILE, arts["bug"])
    problems = []
    for key, (label, value) in needs.items():
        if value is None:
            problems.append(f"{label} missing from artifacts")
        elif key not in recorded:
            problems.append(f"no recorded {key} for {label}; cannot prove it is what was checked")
    return problems


def _recorded_ref(run_entries: list[dict], override: Optional[str]) -> str:
    if override:
        return override
    for stage in ("oracle", "inspect_diff"):
        entry = _last(run_entries, stage)
        if entry and (entry.get("evidence") or {}).get("ref"):
            return str(entry["evidence"]["ref"])
    return DEFAULT_REF


def rerun_inspect(diff: str, bug: dict, frozen_test: str, ref: str):
    """inspect_diff exactly as the runner calls it: traceback from the bug report, frozen test text, and a
    worktree of the tested ref for the AST / pre-image checks."""
    wt = workspace.fresh_worktree(ref)
    try:
        return inspect_diff.inspect(diff, traceback=(bug or {}).get("traceback", ""), frozen_test=frozen_test,
                                    worktree=wt)
    finally:
        workspace.remove_worktree(wt)


def replay(run_id: str, *, audit_path: Path | str | None = None, artifacts_root: Path | str | None = None,
           ref: Optional[str] = None, hmac_key: Optional[str] = None) -> dict:
    """Re-derive a run's decision. `ref` overrides the recorded ref; `hmac_key` defaults to AUDIT_HMAC_KEY."""
    result: dict[str, Any] = {"run_id": run_id, "chain_ok": False, "first_bad_seq": None, "checks": [],
                              "reproduced": False, "reasons": []}
    key = hmac_key if hmac_key is not None else os.environ.get("AUDIT_HMAC_KEY") or None
    audit_path = Path(audit_path or audit.audit_path())
    artifacts_root = Path(artifacts_root or audit.runs_dir() / "artifacts")
    entries = load_audit(audit_path)

    # 1. Chain + HMACs over the whole log.
    if not key:
        result["reasons"].append("AUDIT_HMAC_KEY not set; cannot verify HMACs")
        return result
    ok, bad, why = check_chain(audit_path, entries, key)
    result["chain_ok"], result["first_bad_seq"] = ok, bad
    if not ok:
        result["reasons"].append(why)
        return result

    run_entries = [e for e in entries if e.get("run_id") == run_id]
    if not run_entries:
        result["reasons"].append(f"no audit entries for run {run_id}")
        return result
    stages = [s for s in REPLAYED_STAGES if _last(run_entries, s)]
    result["ref"] = _recorded_ref(run_entries, ref)

    # 2. Artifacts are byte-for-byte what was checked.
    arts, problems = _load_artifacts(run_entries, Path(artifacts_root) / run_id)
    if stages:
        problems += _needed_problems(arts, stages, run_entries)
    if problems:
        result["reasons"] += problems
        return result

    # 3. Re-run the deterministic gates the run reached; compare statuses.
    for stage in stages:
        recorded = _last(run_entries, stage)
        try:
            if stage == "inspect_diff":
                verdict = rerun_inspect(arts["diff"], arts["bug"], arts["test_text"], result["ref"])
            else:
                with tempfile.TemporaryDirectory(prefix="replay_oracle_") as tmp:
                    verdict = oracle.check(arts["diff"], arts["test_path"], ref=result["ref"], artifacts_dir=Path(tmp))
            replayed, replayed_reasons = verdict.status, list(verdict.reasons)
        except Exception as exc:  # noqa: BLE001 - a gate that cannot run is not a reproduction
            replayed, replayed_reasons = "unsure", [f"replay error: {type(exc).__name__}: {exc}"[:300]]
        match = replayed == recorded.get("status")
        result["checks"].append({"stage": stage, "seq": recorded.get("seq"), "recorded": recorded.get("status"),
                                 "replayed": replayed, "match": match, "replayed_reasons": replayed_reasons})
        if not match:
            result["reasons"].append(f"{stage} (seq {recorded.get('seq')}): recorded {recorded.get('status')}, "
                                     f"replayed {replayed}" + (f": {'; '.join(replayed_reasons)}"[:300]
                                                              if replayed_reasons else ""))
    if not stages:
        result["reasons"].append("run ended before inspect_diff; no deterministic gate to re-run (chain and "
                                 "artifact hashes verified)")
    result["reproduced"] = all(c["match"] for c in result["checks"])
    return result


def format_result(result: dict) -> list[str]:
    lines = [f"replay {result['run_id']}"]
    if result["chain_ok"]:
        lines.append("chain: ok (hashes and HMACs verified)")
    elif result["first_bad_seq"] is not None:
        lines.append(f"chain: BROKEN at seq {result['first_bad_seq']}")
    if result.get("ref"):
        lines.append(f"ref: {result['ref']}")
    for c in result["checks"]:
        lines.append(f"  {c['stage']:<13} seq {c['seq']}: recorded {c['recorded']:<6} replayed {c['replayed']:<6} "
                     + ("match" if c["match"] else "MISMATCH"))
    if result["reproduced"]:
        lines.append("decision reproduced")
    else:
        lines += [f"  - {r}" for r in result["reasons"]]
        lines.append("decision NOT reproduced")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factory.replay", description=__doc__.splitlines()[0])
    parser.add_argument("run_id")
    parser.add_argument("--audit")
    parser.add_argument("--artifacts")
    parser.add_argument("--ref", default=None, help="override the recorded ref the oracle replays against")
    args = parser.parse_args(argv)
    result = replay(args.run_id, audit_path=args.audit, artifacts_root=args.artifacts, ref=args.ref)
    print("\n".join(format_result(result)))
    return 0 if result["reproduced"] else 1


if __name__ == "__main__":
    sys.exit(main())
