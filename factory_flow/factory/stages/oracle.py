"""Stage 8: repro red to green, suite stays green (PRD B4).

check(diff, repro_test) runs two fresh worktrees of `prod`:
  1. baseline: full suite (`tests`) -> set of baseline-green nodeids; the frozen repro test
     must be RED there (assertion failure, not a collection/setup error).
  2. patched: apply the diff, the repro must pass, and every baseline-green test must stay green.
     A test that fails on the patched copy is rerun once on the baseline worktree; if it fails
     there too it is flaky (or environment-broken), excluded, and listed in evidence["flaky_excluded"].

Diff integrity (PRD 7.6): evidence["diff_sha256"] = diff_hash(diff) = sre_base.canonical_hash(diff),
i.e. sha256 of json.dumps(diff, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
over the exact diff string (no newline/whitespace normalisation). Canary and promote recompute it
with diff_hash(state.diff).

Patterned on SRE-agent/agent/approval.py @ b4f7e0c (verify_proposal_artifact: record a canonical_hash
of what was checked so later stages can refuse if what they run differs).
Patterned on SRE-agent/evaluation/runner.py @ b4f7e0c (save_payload: results written as files under an
output dir and their resolved paths returned for the record).
New code: the red/green/regression logic itself (no equivalent in SRE-agent).
"""
from __future__ import annotations

import hashlib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from factory import releases, workspace
from factory.contracts import PytestResult, RunState, Verdict
from factory.sre_base import canonical_hash

STAGE = "oracle"
SUITE = "tests"
REPRO_DIR = "oracle_repro"   # outside tests/, so the suite selector never picks the repro up
CHECK_NAMES = ("diff_applies", "repro_red_on_baseline", "repro_now_passes", "suite_still_green")


def diff_hash(diff: str) -> str:
    """sre_base.canonical_hash of the exact diff text (sha256 of its JSON encoding)."""
    return canonical_hash(diff)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _passed_nodeids(result: PytestResult) -> set[str]:
    """Nodeids that passed, read from the junit XML (parse_junit only lists failures)."""
    passed: set[str] = set()
    try:
        root = ET.parse(result["junit_path"]).getroot()
    except (ET.ParseError, OSError):
        return passed
    for case in root.iter("testcase"):
        if any(child.tag in ("failure", "error", "skipped") for child in case):
            continue
        passed.add(workspace._nodeid(case.get("file") or "", case.get("classname") or "", case.get("name") or ""))
    return passed


def _install_repro(worktree: Path, repro_test: Path) -> str:
    """Copy the frozen repro test into <worktree>/oracle_repro/ next to the suite conftest; return its selector."""
    dest_dir = worktree / REPRO_DIR
    dest_dir.mkdir(exist_ok=True)
    for conftest in (worktree / "tests" / "conftest.py", worktree / "fallback_tests" / "conftest.py",
                     Path(repro_test).parent / "conftest.py"):
        if conftest.exists():
            shutil.copyfile(conftest, dest_dir / "conftest.py")
            break
    dest = dest_dir / Path(repro_test).name
    shutil.copyfile(repro_test, dest)
    return f"{REPRO_DIR}/{dest.name}"


def _record(result: PytestResult, label: str, artifacts_dir: Path | None, junits: dict[str, Any]) -> None:
    src = Path(result["junit_path"])
    if not src.exists():
        return
    path = src
    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / f"oracle_{label}.xml"
        shutil.copyfile(src, path)
    junits[label] = {"path": str(path.resolve()), "sha256": _sha256_file(path)}


def _counts(result: PytestResult) -> dict[str, int]:
    return {k: result[k] for k in ("passed", "failed", "errors", "skipped")}


def check(diff: str, repro_test: Path, *, ref: str = "prod", artifacts_dir: Path | None = None) -> Verdict:
    diff = diff or ""
    repro_test = Path(repro_test)
    artifacts_dir = Path(artifacts_dir) if artifacts_dir is not None else None
    checks = {name: False for name in CHECK_NAMES}
    reasons: list[str] = []
    junits: dict[str, Any] = {}
    evidence: dict[str, Any] = {"diff_sha256": diff_hash(diff), "ref": ref, "junit": junits}
    unsure = False

    def verdict() -> Verdict:
        if all(checks.values()):
            status = "pass"
        elif unsure:
            status = "unsure"
        else:
            status = "fail"
        main = junits.get("patched_suite") or junits.get("baseline_suite")
        if main:
            evidence["junit_sha256"] = main["sha256"]
        return Verdict(stage=STAGE, status=status, reasons=reasons, checks=dict(checks), evidence=evidence)

    if not repro_test.is_file():
        reasons.append(f"repro test missing: {repro_test}")
        unsure = True
        return verdict()
    evidence["test_sha256"] = canonical_hash(repro_test.read_text())   # same as reproduce's state.test_sha256
    evidence["repro_test"] = str(repro_test.resolve())

    base: Path | None = None
    patched: Path | None = None
    try:
        # 1. baseline: suite green set, repro must be red (failed, not errored)
        base = workspace.fresh_worktree(ref)
        base_suite = workspace.run_pytest(base, SUITE)
        _record(base_suite, "baseline_suite", artifacts_dir, junits)
        baseline_green = _passed_nodeids(base_suite)
        evidence["baseline"] = _counts(base_suite)
        repro_sel = _install_repro(base, repro_test)
        base_repro = workspace.run_pytest(base, repro_sel)
        _record(base_repro, "baseline_repro", artifacts_dir, junits)
        evidence["baseline_repro"] = _counts(base_repro)
        if base_repro["errors"]:
            unsure = True
            reasons.append("repro test errors on baseline (collection/setup error), not a valid red: "
                           + "; ".join(f"{f['nodeid']}: {f['type']}" for f in base_repro["failures"])[:300])
        elif base_repro["failed"] == 0:
            unsure = True
            reasons.append("repro test passes on baseline; it does not reproduce the bug")
        else:
            checks["repro_red_on_baseline"] = True

        # 2. patched copy: diff applies, repro green, suite keeps every baseline-green test
        patched = workspace.fresh_worktree(ref)
        if diff.strip():
            applied = workspace.apply_diff(patched, diff)
        else:
            applied = {"applied": True, "error": ""}   # empty diff: a no-op patch
            evidence["empty_diff"] = True
        if not applied["applied"]:
            reasons.append(f"diff does not apply: {applied['error']}"[:400])
            return verdict()
        checks["diff_applies"] = True

        repro_sel = _install_repro(patched, repro_test)
        new_repro = workspace.run_pytest(patched, repro_sel)
        _record(new_repro, "patched_repro", artifacts_dir, junits)
        evidence["patched_repro"] = _counts(new_repro)
        if new_repro["passed"] >= 1 and new_repro["failed"] == 0 and new_repro["errors"] == 0:
            checks["repro_now_passes"] = True
        else:
            reasons.append("repro still red: " + ("; ".join(
                f"{f['nodeid']}: {f['type']}" for f in new_repro["failures"]) or "no passing test")[:300])

        new_suite = workspace.run_pytest(patched, SUITE)
        _record(new_suite, "patched_suite", artifacts_dir, junits)
        evidence["patched"] = _counts(new_suite)
        lost = sorted(baseline_green - _passed_nodeids(new_suite))
        flaky: list[str] = []
        regressions: list[str] = []
        for nodeid in lost:
            rerun = workspace.run_pytest(base, nodeid)   # rerun once on the unpatched copy
            (flaky if rerun["passed"] == 0 else regressions).append(nodeid)
        evidence["flaky_excluded"] = flaky
        evidence["regressions"] = regressions
        if not baseline_green:
            unsure = True
            reasons.append("no baseline-green tests; suite gives no evidence")
        elif regressions:
            reasons.append("regression: " + ", ".join(regressions))
        else:
            checks["suite_still_green"] = True
        return verdict()
    except Exception as exc:  # noqa: BLE001 - missing evidence is "unsure", never a crash
        unsure = True
        reasons.append(f"oracle error: {type(exc).__name__}: {exc}"[:400])
        return verdict()
    finally:
        for path in (patched, base):
            if path is not None:
                try:
                    workspace.remove_worktree(path)
                except Exception:  # noqa: BLE001
                    pass


def run(state: RunState) -> Verdict:
    if state.failing_test is None:
        return Verdict(stage=STAGE, status="unsure", reasons=["no frozen repro test in state"],
                       checks={name: False for name in CHECK_NAMES},
                       evidence={"diff_sha256": diff_hash(state.diff or "")})
    try:   # record the commit tested, not the moving branch, so replay re-runs the same code
        ref = releases.rev("prod")
    except RuntimeError:
        ref = "prod"
    verdict = check(state.diff, Path(state.failing_test), ref=ref, artifacts_dir=state.artifacts_dir)
    if verdict.status == "pass":
        state.diff_sha256 = verdict.evidence["diff_sha256"]
    return verdict
