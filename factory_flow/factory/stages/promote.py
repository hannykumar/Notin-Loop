"""Stage 10: commit, tag, restart, watch window (PRD B5).

run(state, watcher=None) -> Verdict("promote")
  - refuses a diff whose canonical_hash differs from state.diff_sha256 (oracle);
  - refuses a diff_sha256 already in the ledger or already claimed (runs/claims/<hash>.json);
  - needs a pre-release baseline with samples > 0 ("unsure" otherwise, nothing changed);
  - in a fresh worktree of prod: apply diff, commit "release-N: <run_id>", tag release-N, then move
    target_service's `prod` branch to it (update-ref compare-and-swap);
  - ledger: Release(status="watching", files_touched, baseline); then deploy_stable("release-N");
  - sets state.release. A failure after the ledger append returns route="rollback".

watch(release, watcher=None, bug_signature="") -> Verdict("watch")
  For WATCH_WINDOW_S after stable restarted, every SCRAPE_INTERVAL_S:
  - the original bug signature must be absent;
  - no NEW signature (absent in the baseline period before promotion) in a file the release touched;
  - SLO vs the pre-release baseline (judge_slo: classify_verification_outcome + absolute caps), sampled
    once a full ALERT_WINDOW_S of post-restart traffic exists.
  Clean -> ledger "kept", pass. Otherwise status fail (or unsure: no traffic) with route="rollback";
  the ledger stays "watching" for rollback (B6).

watch_stage(state, watcher=None) is the Stage-shaped wrapper (uses state.release and state.bug).

Deployment model: see factory/releases.py.

Patterned on SRE-agent @ b4f7e0c:
- executor/service.py _claim: an operation keyed by a hash is claimed (O_EXCL file) before it runs and is
  never run twice; a second attempt is refused and points at the existing claim.
- agent/approval.py verify_proposal_artifact: recompute the hash, refuse on mismatch, before acting.
- executor/engine.py collect_stabilized_evidence: the watch loop (deadline + interval, timestamped samples).
- classify_verification_outcome is the port in factory/sre_base.py.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

from factory import releases, workspace
from factory.contracts import Release, RunState, Verdict
from factory.policy import ALERT_WINDOW_S, SCRAPE_INTERVAL_S, WATCH_WINDOW_S
from factory.stages.canary import hash_check, judge_slo, sample_offsets
from factory.stages.inspect_diff import DiffParseError, parse_unified_diff
from factory.stub_watcher import default_watcher, watcher_signatures

STAGE = "promote"
WATCH_STAGE = "watch"
CLAIMS_DIR = releases.RUNS_DIR / "claims"
GIT_IDENTITY = ["-c", "user.name=sre-factory", "-c", "user.email=sre-factory@localhost"]


def _verdict(stage: str, status: str, reasons: list[str], checks: dict, evidence: dict,
             route: Optional[str] = None) -> Verdict:
    return Verdict(stage=stage, status=status, reasons=reasons, checks=checks,  # type: ignore[arg-type]
                   evidence=evidence, route=route)


def files_touched(diff: str) -> list[str]:
    files: list[str] = []
    for f in parse_unified_diff(diff):
        for p in (f.old_path, f.new_path):
            if p and p not in files:
                files.append(p)
    return files


def claim(diff_sha256: str, run_id: str) -> Optional[dict]:
    """Claim the promotion of diff_sha256. Returns None if claimed now, else the existing claim."""
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    path = CLAIMS_DIR / f"{diff_sha256}.json"
    record = {"operation": "promote", "diff_sha256": diff_sha256, "run_id": run_id,
              "claimed_at": releases.utc_iso(), "status": "in_progress"}
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"diff_sha256": diff_sha256, "status": "uncertain"}
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    return None


def _set_claim(diff_sha256: str, **fields: Any) -> None:
    path = CLAIMS_DIR / f"{diff_sha256}.json"
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        record = {}
    path.write_text(json.dumps({**record, **fields}))


def run(state: RunState, watcher: Any = None) -> Verdict:
    watcher = watcher or default_watcher()
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}

    ok, actual, why = hash_check(state)
    checks["hash_matches"] = ok
    evidence["diff_sha256"] = actual
    if not ok:
        return _verdict(STAGE, "fail", [why], checks, evidence)

    existing = releases.by_hash(actual)
    checks["not_already_released"] = existing is None
    if existing is not None:
        return _verdict(STAGE, "fail", [f"diff {actual[:12]} already released as {existing.tag}"], checks, evidence)

    try:
        touched = files_touched(state.diff)
    except DiffParseError as exc:
        checks["diff_parses"] = False
        return _verdict(STAGE, "fail", [f"diff does not parse: {exc}"], checks, evidence)

    baseline = watcher.current("*", "stable")
    evidence["baseline"] = baseline
    checks["baseline_has_samples"] = baseline.get("samples", 0) > 0
    if not checks["baseline_has_samples"]:
        return _verdict(STAGE, "unsure", ["no stable traffic for a pre-release baseline"], checks, evidence)

    prev_tag = releases.previous_tag()
    try:
        old_prod = releases.rev("prod")
        prev_commit = releases.rev(prev_tag)
    except RuntimeError as exc:
        return _verdict(STAGE, "unsure", [f"cannot resolve prod/{prev_tag}: {exc}"], checks, evidence)
    checks["prod_matches_ledger"] = old_prod == prev_commit
    if not checks["prod_matches_ledger"]:
        return _verdict(STAGE, "unsure", [f"prod ({old_prod[:10]}) is not {prev_tag} ({prev_commit[:10]}): "
                                          "prod moved outside the ledger"], checks, evidence)

    claimed = claim(actual, state.run_id)
    checks["claimed"] = claimed is None
    if claimed is not None:
        evidence["existing_claim"] = claimed
        return _verdict(STAGE, "fail", [f"promotion of {actual[:12]} already claimed "
                                        f"(status {claimed.get('status')}); never run twice"], checks, evidence)

    tag = releases.next_tag(existing_tags=releases.git_tags())
    path = None
    try:
        path = workspace.fresh_worktree(old_prod)
        applied = workspace.apply_diff(path, state.diff)
        checks["diff_applies"] = bool(applied["applied"])
        if not applied["applied"]:
            _set_claim(actual, status="failed", error=applied["error"])
            return _verdict(STAGE, "fail", [f"diff does not apply to prod: {applied['error']}"], checks, evidence)
        releases.git("add", "-A", cwd=path)
        releases.git(*GIT_IDENTITY, "commit", "-q", "-m", f"{tag}: {state.run_id}",
                     "-m", f"diff_sha256: {actual}", cwd=path)
        commit = releases.git("rev-parse", "HEAD", cwd=path)
        releases.git("tag", tag, commit)
        releases.git("update-ref", "refs/heads/prod", commit, old_prod)
    except RuntimeError as exc:
        _set_claim(actual, status="failed", error=str(exc))
        checks["committed"] = False
        return _verdict(STAGE, "fail", [f"commit/tag failed, prod unchanged: {exc}"], checks, evidence)
    finally:
        if path is not None:
            workspace.remove_worktree(path)
    checks["committed"] = True

    now = time.time()
    release = Release(tag=tag, commit=commit, previous_tag=prev_tag, diff_sha256=actual, run_id=state.run_id,
                      promoted_at=releases.utc_iso(now), watch_until=releases.utc_iso(now + WATCH_WINDOW_S),
                      status="watching", files_touched=touched, baseline=dict(baseline))
    releases.append(release)
    state.release = release
    _set_claim(actual, status="released", tag=tag, commit=commit)
    evidence.update({"tag": tag, "commit": commit, "previous_tag": prev_tag, "files_touched": touched})

    try:
        evidence["stable"] = releases.deploy_stable(tag)
        checks["stable_restarted"] = True
    except RuntimeError as exc:
        checks["stable_restarted"] = False
        return _verdict(STAGE, "fail", [f"stable did not restart at {tag}: {exc}"], checks, evidence,
                        route="rollback")
    return _verdict(STAGE, "pass", [], checks, evidence)


def _signature_file(signature: str) -> str:
    """'OrderNotFound@app/orders.py:get_order' -> 'app/orders.py'."""
    where = signature.split("@", 1)[1] if "@" in signature else ""
    return where.split(":", 1)[0]


def watch(release: Release, watcher: Any = None, *, bug_signature: str = "",
          window_s: float = WATCH_WINDOW_S, interval_s: float = SCRAPE_INTERVAL_S,
          judge_after_s: float = ALERT_WINDOW_S, baseline_period_s: float = WATCH_WINDOW_S,
          since_ts: Optional[float] = None, sleep: Callable[[float], None] = time.sleep) -> Verdict:
    watcher = watcher or default_watcher()
    promoted_ts = releases.parse_iso(release.promoted_at)
    if since_ts is None:
        deployed = releases.stable_state()
        since_ts = float(deployed["started_ts"]) if deployed.get("ref") == release.tag else promoted_ts
    baseline_sigs = watcher_signatures(watcher, promoted_ts - baseline_period_s, "stable", promoted_ts)
    touched = set(release.files_touched)
    evidence: dict[str, Any] = {"tag": release.tag, "watch_window_s": window_s, "since_ts": since_ts,
                                "baseline": release.baseline, "baseline_signatures": dict(baseline_sigs),
                                "bug_signature": bug_signature, "files_touched": sorted(touched)}
    checks = {"bug_signature_absent": True, "no_new_signatures_in_touched_files": True, "slo_holds": True}
    reasons: list[str] = []
    samples: list[dict] = []
    other_new: set[str] = set()

    t0 = time.monotonic() - max(time.time() - since_ts, 0.0)
    for offset in sample_offsets(window_s, interval_s, interval_s):
        delay = t0 + offset - time.monotonic()
        if delay > 0:
            sleep(delay)
        now = time.time()
        sigs = watcher_signatures(watcher, since_ts, "stable", now)
        sample: dict[str, Any] = {"offset_s": offset, "observed_at": now, "signatures": dict(sigs)}
        if bug_signature and sigs.get(bug_signature):
            checks["bug_signature_absent"] = False
            reasons.append(f"original bug signature {bug_signature} seen {sigs[bug_signature]}x after release")
        new = {s for s in sigs if s not in baseline_sigs}
        new_touched = sorted(s for s in new if _signature_file(s) in touched)
        other_new |= new - set(new_touched)
        if new_touched:
            checks["no_new_signatures_in_touched_files"] = False
            reasons.append(f"new signature(s) in files the release touched: {', '.join(new_touched)}")
        if offset >= min(judge_after_s, window_s):
            snap = watcher.current("*", "stable")
            sample["current"] = snap
            if snap.get("samples", 0) > 0:
                judged = judge_slo(release.baseline, snap)  # type: ignore[arg-type]
                sample["judgement"] = judged
                if not judged["ok"]:
                    checks["slo_holds"] = False
                    reasons.extend(f"SLO vs pre-release baseline: {r}" for r in judged["reasons"])
        samples.append(sample)
        if reasons:
            break
    evidence["samples"] = samples
    evidence["new_signatures_elsewhere"] = sorted(other_new)

    if reasons:
        return _verdict(WATCH_STAGE, "fail", [f"{release.tag} not clean in watch window; hand to rollback",
                                              *reasons], checks, evidence, route="rollback")
    checks["slo_has_samples"] = any("judgement" in s for s in samples)
    if not checks["slo_has_samples"]:
        return _verdict(WATCH_STAGE, "unsure", ["no stable traffic observed in the watch window"],
                        checks, evidence, route="rollback")
    releases.update(release.tag, status="kept")
    evidence["ledger_status"] = "kept"
    return _verdict(WATCH_STAGE, "pass", [], checks, evidence)


def watch_stage(state: RunState, watcher: Any = None) -> Verdict:
    """Stage-shaped wrapper: watch(state.release) with the bug signature from state.bug."""
    if state.release is None:
        return _verdict(WATCH_STAGE, "unsure", ["no release to watch"], {"release_present": False}, {})
    return watch(state.release, watcher, bug_signature=state.bug.signature if state.bug else "")
