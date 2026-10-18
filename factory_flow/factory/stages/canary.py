"""Stage 9: canary vs stable SLO (PRD B5).

run(state, watcher=None) -> Verdict("canary"):
  1. recompute canonical_hash(state.diff) and refuse on mismatch with state.diff_sha256 (set by the oracle);
  2. fresh worktree of `prod` + the diff, started as instance "canary" on CANARY_PORT;
  3. traffic generator control file -> canary_url / canary_share = CANARY_TRAFFIC_SHARE (80/20 split);
  4. for CANARY_WINDOW_S, sample stable and canary (path "*") every SCRAPE_INTERVAL_S once a full
     watcher window (ALERT_WINDOW_S) of canary traffic exists, and judge each sample with judge_slo();
  5. finally (always): canary_url reset to "", canary stopped, worktree removed.

The watcher scrapes the canary only while it runs (add_target / remove_target), so its "canary" window holds
this canary's traffic only; sampling from ALERT_WINDOW_S onwards waits for a full window. Canary samples == 0
(or stable samples == 0) => "unsure".

Ported/patterned from SRE-agent @ b4f7e0c:
- sre_base.classify_verification_outcome (ported from executor/engine.py) decides "regressed".
- Sampling loop patterned on collect_stabilized_evidence in executor/engine.py (deadline + interval,
  list of timestamped samples kept as evidence).
- Hash check patterned on verify_proposal_artifact in agent/approval.py (recompute canonical_hash of the
  stored artifact and refuse on mismatch before acting).
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any, Callable, Optional

from factory import watcher as _watcher
from factory import workspace
from factory.contracts import MetricsSnapshot, RunState, Verdict
from factory.policy import (
    ALERT_WINDOW_S,
    CANARY_PORT,
    CANARY_TRAFFIC_SHARE,
    CANARY_WINDOW_S,
    SCRAPE_INTERVAL_S,
    SLO_FORBIDDEN_OUTCOMES,
    SLO_MAX_ERROR_RATE_PERCENT,
    SLO_MAX_P95_MS,
)
from factory.sre_base import canonical_hash, classify_verification_outcome

STAGE = "canary"
CANARY_URL = f"http://127.0.0.1:{CANARY_PORT}"
# Local requests take ~2 ms; classify_verification_outcome's tolerance is max(10%, 0.1 ms), so raw p95
# noise (2 ms vs 3 ms) would read as "regressed". p95 values below this floor are clamped to it before
# the comparison; real latency regressions are still caught above the floor and by SLO_MAX_P95_MS.
LATENCY_NOISE_FLOOR_MS = 50.0


# ---------------------------------------------------------------- traffic control

_TRAFFIC_MODULE: Any = None


def _traffic():
    """target_service/traffic/generate.py loaded by path (single source of truth for set_control)."""
    global _TRAFFIC_MODULE
    if _TRAFFIC_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "factory_traffic_generate", workspace.target_dir() / "traffic" / "generate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _TRAFFIC_MODULE = module
    return _TRAFFIC_MODULE


def set_traffic(**updates: Any) -> dict:
    """Merge keys into the traffic generator's control file (TRAFFIC_CONTROL or runs/traffic_control.json)."""
    return _traffic().set_control(**updates)


# ---------------------------------------------------------------- SLO judgement

def hash_check(state: RunState) -> tuple[bool, str, str]:
    actual = canonical_hash(state.diff or "")
    if not state.diff_sha256:
        return False, actual, "no diff_sha256 recorded by the oracle"
    if actual != state.diff_sha256:
        return False, actual, f"diff hash mismatch: oracle {state.diff_sha256[:12]}, now {actual[:12]}"
    return True, actual, ""


def _clamped(snapshot: MetricsSnapshot) -> dict:
    metrics = dict(snapshot.get("metrics") or {})
    if isinstance(metrics.get("p95_latency_ms"), (int, float)):
        metrics["p95_latency_ms"] = max(float(metrics["p95_latency_ms"]), LATENCY_NOISE_FLOOR_MS)
    return {"metrics": metrics}


def judge_slo(before: MetricsSnapshot, after: MetricsSnapshot) -> dict:
    """SLO of `after` (canary / post-release stable) against `before` (stable / pre-release baseline).

    ok = classify_verification_outcome(before, after, {}) not in SLO_FORBIDDEN_OUTCOMES
         and after's error rate <= SLO_MAX_ERROR_RATE_PERCENT and after's p95 <= SLO_MAX_P95_MS.
    """
    outcome = classify_verification_outcome(_clamped(before), _clamped(after), {})
    metrics = after.get("metrics") or {}
    err = metrics.get("error_rate_percent")
    p95 = metrics.get("p95_latency_ms")
    checks = {
        "not_regressed": outcome not in SLO_FORBIDDEN_OUTCOMES,
        "error_rate_within_slo": isinstance(err, (int, float)) and err <= SLO_MAX_ERROR_RATE_PERCENT,
        "p95_within_slo": isinstance(p95, (int, float)) and p95 <= SLO_MAX_P95_MS,
    }
    reasons = []
    if not checks["not_regressed"]:
        reasons.append(f"classify_verification_outcome={outcome} "
                       f"(before {before.get('metrics')}, after {metrics})")
    if not checks["error_rate_within_slo"]:
        reasons.append(f"error rate {err}% > SLO {SLO_MAX_ERROR_RATE_PERCENT}%")
    if not checks["p95_within_slo"]:
        reasons.append(f"p95 {p95} ms > SLO {SLO_MAX_P95_MS} ms")
    return {"ok": all(checks.values()), "outcome": outcome, "checks": checks, "reasons": reasons}


def sample_offsets(window_s: float, interval_s: float, judge_after_s: float) -> list[float]:
    """Seconds after start at which to sample: from min(judge_after_s, window_s) to window_s."""
    first = min(judge_after_s, window_s)
    offsets, t = [], first
    while t < window_s - 1e-9:
        offsets.append(round(t, 3))
        t += interval_s
    offsets.append(float(window_s))
    return offsets


# ---------------------------------------------------------------- stage

def _verdict(status: str, reasons: list[str], checks: dict, evidence: dict) -> Verdict:
    return Verdict(stage=STAGE, status=status, reasons=reasons, checks=checks, evidence=evidence)  # type: ignore[arg-type]


def run(state: RunState, watcher: Any = None, *, window_s: float = CANARY_WINDOW_S,
        interval_s: float = SCRAPE_INTERVAL_S, judge_after_s: float = ALERT_WINDOW_S,
        share: float = CANARY_TRAFFIC_SHARE, sleep: Callable[[float], None] = time.sleep) -> Verdict:
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {"canary_window_s": window_s, "canary_share": share}

    ok, actual, why = hash_check(state)
    checks["hash_matches"] = ok
    evidence["diff_sha256"] = actual
    if not ok:
        return _verdict("fail", [why], checks, evidence)

    watcher = watcher or _watcher.default()
    path: Optional[Path] = None
    started = False
    try:
        try:
            path = workspace.fresh_worktree("prod")
        except RuntimeError as exc:
            checks["canary_started"] = False
            return _verdict("unsure", [f"cannot create prod worktree: {exc}"], checks, evidence)
        applied = workspace.apply_diff(path, state.diff)
        checks["diff_applies"] = bool(applied["applied"])
        if not applied["applied"]:
            return _verdict("fail", [f"diff does not apply to prod: {applied['error']}"], checks, evidence)
        try:
            evidence["canary_instance"] = workspace.start_instance(path, CANARY_PORT, "canary")
            started = True
        except RuntimeError as exc:
            checks["canary_started"] = False
            return _verdict("unsure", [f"canary did not start: {exc}"], checks, evidence)
        checks["canary_started"] = True
        watcher.add_target("canary", CANARY_URL)

        set_traffic(canary_url=CANARY_URL, canary_share=share)
        t0 = time.monotonic()
        evidence["canary_started_at"] = time.time()
        samples: list[dict] = []
        failed: list[str] = []
        for offset in sample_offsets(window_s, interval_s, judge_after_s):
            delay = t0 + offset - time.monotonic()
            if delay > 0:
                sleep(delay)
            stable = watcher.current("*", "stable")
            canary = watcher.current("*", "canary")
            sample: dict[str, Any] = {"offset_s": offset, "observed_at": time.time(),
                                      "stable": stable, "canary": canary}
            if canary.get("samples", 0) > 0 and stable.get("samples", 0) > 0:
                judged = judge_slo(stable, canary)
                sample["judgement"] = judged
                if not judged["ok"]:
                    failed = [f"at {offset:g}s: {r}" for r in judged["reasons"]]
            samples.append(sample)
            if failed:
                break
        evidence["samples"] = samples
        judged_samples = [s for s in samples if "judgement" in s]
        checks["canary_has_traffic"] = any(s["canary"].get("samples", 0) > 0 for s in samples)
        checks["stable_has_traffic"] = any(s["stable"].get("samples", 0) > 0 for s in samples)
        if failed:
            last = judged_samples[-1]["judgement"]["checks"]
            checks.update({f"canary_{k}": v for k, v in last.items()})
            return _verdict("fail", ["canary SLO slipped vs stable; canary killed", *failed], checks, evidence)
        if not judged_samples:
            return _verdict("unsure", ["no canary/stable traffic observed in the canary window"], checks, evidence)
        checks.update({"canary_not_regressed": True, "canary_error_rate_within_slo": True,
                       "canary_p95_within_slo": True})
        return _verdict("pass", [], checks, evidence)
    finally:
        try:
            set_traffic(canary_url="")
        finally:
            watcher.remove_target("canary")
            if started or "canary" in workspace.instances():
                workspace.stop_instance("canary")
            if path is not None:
                workspace.remove_worktree(path)
