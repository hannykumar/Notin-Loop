"""Stage 11: previous tag or git revert, verify health (PRD B6, §7.6).

rollback(release_tag, *, actor="agent", ...) -> Verdict("rollback")
  1. Scope: only releases in runs/releases.json (made by the agent) can be rolled back; unknown or already
     rolled_back -> fail, nothing changed. actor="agent" additionally needs the release to be "watching"
     or promoted within ROLLBACK_LOOKBACK_S (the agent only undoes its own *recent* release);
     actor="human" may roll back any ledger release.
  2. Method:
     - checkout_previous_tag: prod points at the release's commit -> `git update-ref refs/heads/prod
       <previous_tag commit> <release commit>` (compare-and-swap) and deploy_stable(previous_tag).
     - git_revert: the release is an ancestor of prod (newer releases on top) -> in a fresh worktree of
       prod, `git revert --no-edit <release.commit>`. Conflict -> `git revert --abort`, nothing changed
       (no ref, tag, ledger or deploy change), Verdict fail, critical inbox case. Success -> the revert
       commit is tagged as the NEXT release tag (release-N) and appended to the ledger as a Release with
       status "kept" (previous_tag = the tag prod was at, diff_sha256 = hash of the revert diff), so the
       ledger invariant promote relies on (prod == ledger's latest kept/watching tag) keeps holding;
       then prod is moved (compare-and-swap) and deploy_stable(new tag). The newer releases stay.
  3. Verify health (after the restart):
     - probes: each GET request (path + query) that produced one of the release's new signatures on stable
       during its watch period, and is a policy.SAFE_REPLAY_PATHS path, is sent PROBE_COUNT times to stable
       right after the restart (e.g. /reports/daily for demo 3 instead of waiting ~20 s for the scheduled
       hit); any 5xx -> not restored;
     - settle: wait until settle_s after stable restarted, so the watcher's window holds post-restart
       traffic only;
     - signatures: none of the release's new signatures (passed in, else computed as in promote.watch:
       absent in the baseline period before promotion, seen afterwards, in a file the release touched, not
       the original bug signature) appear in stable's log since the restart;
     - SLO: canary.judge_slo(release.baseline, stable current) (classify_verification_outcome not in
       SLO_FORBIDDEN_OUTCOMES + absolute caps). A rollback deliberately reopens the original bug, whose
       errors were already in the pre-release baseline; if the plain judgement fails and the bug's
       request path is known, errors on that path are subtracted (per-path watcher snapshots, counts =
       rate x samples) and the remainder is judged again. Both judgements are kept as evidence.
     - no stable samples / new signatures but nothing to probe -> unsure.
  4. After rollback: ledger status "rolled_back" (as soon as the code is rolled back, whatever the health
     result); an inbox case reopening the bug with the failed fix (release, diff_sha256, new signatures,
     verification); runs/PAUSED (reason, release, ts) after an agent rollback. Health not restored or not
     verifiable, revert conflict, or a failed restart -> critical case, PAUSED, Verdict fail/unsure
     ("pause line"). Health restored -> Verdict pass (the pipeline still escalates: the bug is open).
  5. Evidence: method, prod commit before/after, deployed ref, verification, signatures checked, inbox case
     path, paused path, actor.

run(state, watcher=None) uses state.release and state.bug. Manual path:
    python -m factory.stages.rollback release-N      -> rollback(tag, actor="human"), same code.

Audit: in a pipeline run the runner audits the Verdict; a manual call passes audit_append=audit.append
(main() does) to have it appended (entry fields per PRD 7.7: run_id, stage, actor, status, reasons, checks, evidence).
Inbox: inbox.open_case writes runs/inbox/<run_id>.json; the runner's escalation merges into the same case.

Ported/patterned from SRE-agent @ b4f7e0c:
- executor/engine.py execute_rollback / executor/service.py rollback: pick the rollback action for what was
  applied, execute it, then record the result; "not available" when there is nothing to roll back.
- runtime/storage.py record_rollback / update_latest_rollback_status: record the rollback, then update the
  record's status in place (here: releases.update(tag, status="rolled_back") on the JSON ledger).
- classify_verification_outcome (ported in factory/sre_base.py from executor/engine.py) via canary.judge_slo.
New code: git revert / tag / ref moves, probes, inbox case and PAUSED file.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from factory import audit, inbox, releases, workspace
from factory import watcher as _watcher
from factory.contracts import BugReport, Release, RunState, Verdict
from factory.policy import (
    ALERT_WINDOW_S,
    ROLLBACK_LOOKBACK_S,
    SAFE_REPLAY_METHODS,
    SAFE_REPLAY_PATHS,
    SCRAPE_INTERVAL_S,
    SLO_MAX_ERROR_RATE_PERCENT,
    STABLE_PORT,
    WATCH_WINDOW_S,
)
from factory.sre_base import canonical_hash
from factory.stages.canary import judge_slo
from factory.stages.promote import GIT_IDENTITY, _signature_file
from factory.watcher import signature

STAGE = "rollback"
STABLE_URL = f"http://127.0.0.1:{STABLE_PORT}"
SETTLE_S = ALERT_WINDOW_S + SCRAPE_INTERVAL_S   # full watcher window of post-restart traffic
PROBE_COUNT = 3
MAX_PROBES = 3                                     # distinct (path, query) probes


def _verdict(status: str, reasons: list[str], checks: dict, evidence: dict) -> Verdict:
    return Verdict(stage=STAGE, status=status, reasons=reasons, checks=checks, evidence=evidence)  # type: ignore[arg-type]


# ---------------------------------------------------------------- helpers

def http_probe(path: str, query: Optional[dict] = None) -> int:
    """GET stable; returns the HTTP status (0 = no response)."""
    import httpx

    try:
        return httpx.get(f"{STABLE_URL}{path}", params=query or None, timeout=5.0).status_code
    except httpx.HTTPError:
        return 0


def _safe_path(method: str, path: str) -> bool:
    return method in SAFE_REPLAY_METHODS and any(p.match(path or "") for p in SAFE_REPLAY_PATHS)


def _watch_period(release: Release) -> tuple[float, float]:
    start = releases.parse_iso(release.promoted_at)
    end = time.time() if release.status == "watching" else max(releases.parse_iso(release.watch_until),
                                                                start + WATCH_WINDOW_S)
    return start, min(end, time.time())


def release_new_signatures(release: Release, watcher: Any, bug_signature: str = "") -> list[str]:
    """Signatures seen on stable during the release's watch period, absent in the WATCH_WINDOW_S before
    promotion, in a file the release touched, excluding the original bug signature (same rule as watch)."""
    start, end = _watch_period(release)
    before = watcher.signatures(start - WATCH_WINDOW_S, "stable", start)
    after = watcher.signatures(start, "stable", end)
    touched = set(release.files_touched)
    return sorted(s for s in after if s not in before and s != bug_signature
                  and (not touched or _signature_file(s) in touched))


def probe_requests(release: Release, watcher: Any, signatures: list[str]) -> list[dict]:
    """Distinct safe GET requests that produced one of `signatures` on stable during the watch period."""
    if not signatures:
        return []
    start, end = _watch_period(release)
    wanted, found = set(signatures), []
    for e in watcher.log_lines(start, end, instance="stable"):
        if int(e.get("status", 0)) < 500 or signature(e) not in wanted:
            continue
        req = {"path": e.get("path", ""), "query": dict(e.get("query") or {})}
        if _safe_path(e.get("method", ""), req["path"]) and req not in found:
            found.append(req)
        if len(found) >= MAX_PROBES:
            break
    return found


def _excluding_paths(watcher: Any, snap: dict, paths: list[str]) -> Optional[dict]:
    """stable '*' snapshot minus requests on `paths` (counts = rate x samples); p95 kept (conservative)."""
    total = int(snap.get("samples", 0))
    errors = float((snap.get("metrics") or {}).get("error_rate_percent", 0.0)) * total / 100.0
    for p in paths:
        s = watcher.current(p, "stable")
        n = int(s.get("samples", 0))
        total -= n
        errors -= float((s.get("metrics") or {}).get("error_rate_percent", 0.0)) * n / 100.0
    if total <= 0:
        return None
    metrics = dict(snap.get("metrics") or {})
    metrics["error_rate_percent"] = round(max(errors, 0.0) * 100.0 / total, 3)
    return {**snap, "metrics": metrics, "samples": total, "excluded_paths": list(paths)}


def verify_health(release: Release, watcher: Any, *, deployed_ts: float, new_signatures: list[str],
                  probes: list[dict], reopened_paths: list[str], settle_s: float,
                  sleep: Callable[[float], None], probe: Callable[..., int]) -> dict:
    """Returns {"status": pass|fail|unsure, "reasons", "checks", ...evidence}."""
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    out: dict[str, Any] = {"new_signatures": list(new_signatures), "probes": []}

    for req in probes:
        statuses = [probe(req["path"], req["query"]) for _ in range(PROBE_COUNT)]
        out["probes"].append({**req, "statuses": statuses})
    if new_signatures:
        checks["new_signature_paths_probed"] = bool(probes)
        if not probes:
            reasons.append("no safe request known that produced the release's new signatures; cannot probe")
    if probes:
        checks["probes_ok"] = all(0 < s < 500 for p in out["probes"] for s in p["statuses"])
        if not checks["probes_ok"]:
            reasons.append(f"probes after rollback still fail: {out['probes']}")

    delay = deployed_ts + settle_s - time.time()
    if delay > 0:
        sleep(delay)
    now = time.time()
    after_sigs = watcher.signatures(deployed_ts, "stable", now)
    out["signatures_after_restart"] = dict(after_sigs)
    still = sorted(s for s in new_signatures if after_sigs.get(s))
    checks["new_signatures_gone"] = not still
    if still:
        reasons.append(f"release's new signature(s) still seen after rollback: {', '.join(still)}")

    snap = watcher.current("*", "stable")
    out["current"] = snap
    out["baseline"] = release.baseline
    checks["stable_has_samples"] = snap.get("samples", 0) > 0
    checks["baseline_has_samples"] = (release.baseline or {}).get("samples", 0) > 0
    if not checks["stable_has_samples"] or not checks["baseline_has_samples"]:
        reasons.append("no stable traffic after rollback" if not checks["stable_has_samples"]
                       else "release has no pre-release baseline")
        out.update(status="unsure", reasons=reasons, checks=checks)
        return out
    judged = judge_slo(release.baseline, snap)  # type: ignore[arg-type]
    out["judgement"] = judged
    final = judged
    if not judged["ok"] and reopened_paths:
        adjusted = _excluding_paths(watcher, snap, reopened_paths)
        if adjusted is not None:
            out["current_excluding_reopened_bug"] = adjusted
            final = judge_slo(release.baseline, adjusted)  # type: ignore[arg-type]
            out["judgement_excluding_reopened_bug"] = final
    out["outcome"] = final["outcome"]
    checks.update({f"health_{k}": v for k, v in final["checks"].items()})
    if not final["ok"]:
        reasons.extend(f"SLO vs pre-release baseline: {r}" for r in final["reasons"])

    hard_fail = any(checks.get(k) is False for k in ("probes_ok", "new_signatures_gone")) or not final["ok"]
    status = "fail" if hard_fail else ("unsure" if checks.get("new_signature_paths_probed") is False else "pass")
    out.update(status=status, reasons=reasons, checks=checks)
    return out


def _tag_at(commit: str) -> str:
    for r in reversed(releases.load()):
        if r.commit == commit and r.status != "rolled_back":
            return r.tag
    try:
        if releases.rev(releases.INITIAL_TAG) == commit:
            return releases.INITIAL_TAG
    except RuntimeError:
        pass
    return releases.previous_tag()


def _route(watcher: Any, path: str, since_ts: float) -> str:
    """Route template the watcher keys metrics by for a concrete request path (path itself if unseen)."""
    return next((e["route"] for e in watcher.log_lines(since_ts, instance="stable")
                 if e.get("path") == path and e.get("route")), path)


def _rc(args: list[str], cwd: Optional[Path] = None) -> int:
    """git exit code (releases.git raises on failure; revert/merge-base need the code)."""
    return subprocess.run(["git", *args], cwd=str(cwd or workspace.target_dir()), capture_output=True,
                          text=True, timeout=workspace.GIT_TIMEOUT_S).returncode


def _pause(reason: str, tag: str, run_id: str, actor: str) -> Path:
    path = workspace.runs_dir() / "PAUSED"   # same file as pipeline.is_paused()
    path.write_text(json.dumps({"reason": reason, "release": tag, "run_id": run_id, "actor": actor,
                                "ts": releases.utc_iso()}, indent=2))
    return path


# ---------------------------------------------------------------- stage

def rollback(release_tag: str, *, actor: str = "agent", run_id: str = "", reason: str = "",
             watcher: Any = None, new_signatures: Optional[list[str]] = None, bug: Optional[BugReport] = None,
             settle_s: float = SETTLE_S, sleep: Callable[[float], None] = time.sleep,
             probe: Callable[..., int] = http_probe,
             audit_append: Optional[Callable[[dict], Any]] = None) -> Verdict:
    watcher = watcher or _watcher.default()
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {"tag": release_tag, "actor": actor, "reason": reason}
    verdict = _rollback(release_tag, actor, run_id, reason, watcher, new_signatures, bug, settle_s, sleep,
                        probe, checks, evidence)
    if audit_append is not None:
        audit_append({"run_id": evidence.get("run_id", run_id), "stage": STAGE, "actor": actor,
                      "status": verdict.status, "reasons": verdict.reasons, "checks": verdict.checks,
                      "evidence": verdict.evidence})
    return verdict


def _rollback(tag: str, actor: str, run_id: str, reason: str, watcher: Any, new_signatures: Optional[list[str]],
              bug: Optional[BugReport], settle_s: float, sleep: Callable[[float], None], probe: Callable[..., int],
              checks: dict[str, bool], evidence: dict[str, Any]) -> Verdict:
    # 1. scope ---------------------------------------------------------------
    release = releases.get(tag)
    checks["release_in_ledger"] = release is not None
    if release is None:
        return _verdict("fail", [f"{tag} is not in the release ledger: the agent only rolls back its own "
                                 "releases; nothing changed"], checks, evidence)
    run_id = run_id or (f"manual-rollback-{tag}" if actor == "human" else release.run_id)
    evidence.update(run_id=run_id, release=asdict(release))
    checks["not_already_rolled_back"] = release.status != "rolled_back"
    if not checks["not_already_rolled_back"]:
        return _verdict("fail", [f"{tag} is already rolled back; nothing changed"], checks, evidence)
    if actor != "human":
        recent = time.time() - releases.parse_iso(release.promoted_at) <= ROLLBACK_LOOKBACK_S
        checks["agent_may_roll_back"] = release.status == "watching" or recent
        if not checks["agent_may_roll_back"]:
            return _verdict("fail", [f"{tag} is {release.status} and older than ROLLBACK_LOOKBACK_S; only a "
                                     "human rolls it back; nothing changed"], checks, evidence)

    bug_signature = bug.signature if bug else ""
    if new_signatures is None:
        new_signatures = release_new_signatures(release, watcher, bug_signature)
        evidence["new_signatures_source"] = "computed from the watch period"
    else:
        evidence["new_signatures_source"] = "passed in"
    new_signatures = sorted(set(new_signatures))
    evidence["new_signatures"] = new_signatures
    probes = probe_requests(release, watcher, new_signatures)

    def escalate(status: str, severity: str, reasons: list[str], summary: str) -> Verdict:
        case = {"reopened_bug": asdict(bug) if bug else None,
                "failed_fix": {"tag": tag, "commit": release.commit, "diff_sha256": release.diff_sha256,
                               "run_id": release.run_id, "files_touched": release.files_touched,
                               "new_signatures": new_signatures},
                "rollback": {k: v for k, v in evidence.items() if k != "release"},
                "verdict": {"status": status, "reasons": reasons, "checks": dict(checks)}}
        path = inbox.open_case("rollback", severity, run_id, summary, case)
        evidence["inbox_case"] = str(path)
        evidence["severity"] = severity
        if actor != "human":
            evidence["paused"] = str(_pause(summary, tag, run_id, actor))
        return _verdict(status, reasons, checks, evidence)

    # 2. method ---------------------------------------------------------------
    try:
        prod_before = releases.rev("prod")
        release_commit = releases.rev(release.commit)
    except RuntimeError as exc:
        return _verdict("unsure", [f"cannot resolve prod/{tag}: {exc}; nothing changed"], checks, evidence)
    evidence["prod_before"] = prod_before
    if prod_before == release_commit:
        method = "checkout_previous_tag"
    elif _rc(["merge-base", "--is-ancestor", release_commit, prod_before]) == 0:
        method = "git_revert"
    else:
        checks["release_in_prod_history"] = False
        return _verdict("unsure", [f"{tag} ({release_commit[:10]}) is not in prod's history; nothing changed"],
                        checks, evidence)
    evidence["method"] = method

    if method == "checkout_previous_tag":
        deploy_ref = release.previous_tag
        try:
            target = releases.rev(deploy_ref)
            releases.git("update-ref", "refs/heads/prod", target, prod_before)
        except RuntimeError as exc:
            checks["prod_moved"] = False
            return escalate("fail", "critical", [f"cannot move prod to {deploy_ref}: {exc}; nothing changed"],
                            f"rollback of {tag} failed: prod could not be moved")
    else:
        path = workspace.fresh_worktree(prod_before)
        try:
            proc_rc = _rc([*GIT_IDENTITY, "revert", "--no-edit", release_commit], cwd=path)
            if proc_rc != 0:
                conflicts = releases.git("diff", "--name-only", "--diff-filter=U", cwd=path, check=False)
                _rc(["revert", "--abort"], cwd=path)
                checks["revert_clean"] = False
                evidence["conflicts"] = conflicts.splitlines()
                return escalate("fail", "critical",
                                [f"git revert of {tag} conflicts ({', '.join(evidence['conflicts']) or '?'}); "
                                 "aborted, nothing changed"],
                                f"rollback of {tag} needs a human: git revert conflicts")
            checks["revert_clean"] = True
            deploy_ref = releases.next_tag(existing_tags=releases.git_tags())
            releases.git(*GIT_IDENTITY, "commit", "--amend", "-q", "-m", f"{deploy_ref}: revert {tag} ({run_id})",
                         "-m", f"Reverts {release_commit}. actor: {actor}", cwd=path)
            target = releases.git("rev-parse", "HEAD", cwd=path)
            revert_diff = releases.git("diff", prod_before, target, cwd=path)
            files = [f for f in releases.git("diff", "--name-only", prod_before, target, cwd=path).splitlines() if f]
            releases.git("tag", deploy_ref, target)
            releases.git("update-ref", "refs/heads/prod", target, prod_before)
        except RuntimeError as exc:
            checks["prod_moved"] = False
            return escalate("fail", "critical", [f"git revert of {tag} failed: {exc}"],
                            f"rollback of {tag} failed during git revert")
        finally:
            workspace.remove_worktree(path)
        now = time.time()
        releases.append(Release(tag=deploy_ref, commit=target, previous_tag=_tag_at(prod_before),
                                diff_sha256=canonical_hash(revert_diff), run_id=run_id,
                                promoted_at=releases.utc_iso(now), watch_until=releases.utc_iso(now),
                                status="kept", files_touched=files, baseline={}))
        evidence["revert_tag"] = deploy_ref
    checks["prod_moved"] = True
    evidence["prod_after"] = target
    evidence["deployed_ref"] = deploy_ref
    releases.update(tag, status="rolled_back")
    evidence["ledger_status"] = "rolled_back"

    try:
        deployed = releases.deploy_stable(deploy_ref)
        checks["stable_restarted"] = True
        evidence["stable"] = deployed
    except RuntimeError as exc:
        checks["stable_restarted"] = False
        return escalate("fail", "critical", [f"stable did not restart at {deploy_ref}: {exc}"],
                        f"rollback of {tag}: stable did not restart; pause line")

    # 3. verify ---------------------------------------------------------------
    reopened = ([_route(watcher, bug.sample_request["path"], releases.parse_iso(release.promoted_at) - WATCH_WINDOW_S)]
                if bug and bug.sample_request.get("path") else [])
    health = verify_health(release, watcher, deployed_ts=float(deployed.get("started_ts") or time.time()),
                           new_signatures=new_signatures, probes=probes, reopened_paths=reopened,
                           settle_s=settle_s, sleep=sleep, probe=probe)
    evidence["verification"] = {k: v for k, v in health.items() if k not in ("status", "reasons", "checks")}
    evidence["verification_outcome"] = health.get("outcome", "inconclusive")
    evidence["signatures_checked"] = new_signatures
    checks.update(health["checks"])
    checks["health_restored"] = health["status"] == "pass"

    # 4. after ---------------------------------------------------------------
    if health["status"] == "pass":
        return escalate("pass", "high", [f"{tag} rolled back ({method}) to {deploy_ref}; health verified; "
                                         "bug reopened"],
                        f"{tag} rolled back to {deploy_ref}; health restored; bug reopened for a human")
    return escalate(health["status"], "critical",
                    [f"{tag} rolled back ({method}) to {deploy_ref} but health is "
                     f"{'not restored' if health['status'] == 'fail' else 'not verified'}; pause line",
                     *health["reasons"]],
                    f"{tag} rolled back to {deploy_ref} but health not restored; line paused")


def run(state: RunState, watcher: Any = None) -> Verdict:
    """Stage-shaped wrapper: roll back state.release (bug signature/path from state.bug)."""
    if state.release is None:
        return _verdict("unsure", ["no release to roll back"], {"release_present": False}, {})
    return rollback(state.release.tag, actor="agent", run_id=state.run_id, reason="watch window not clean",
                    watcher=watcher, bug=state.bug)


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m factory.stages.rollback release-N", file=sys.stderr)
        return 2
    v = rollback(argv[0], actor="human", reason="manual rollback", audit_append=audit.append)
    print(json.dumps({"stage": v.stage, "status": v.status, "reasons": v.reasons, "checks": v.checks,
                      "evidence": {k: v.evidence.get(k) for k in (
                          "method", "prod_before", "prod_after", "deployed_ref", "verification_outcome",
                          "signatures_checked", "inbox_case", "actor")}}, indent=2, default=str))
    return 0 if v.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
