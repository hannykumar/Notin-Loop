"""Stage 3: signatures, replay, evidence gate, bug report (PRD A7, §7.5)."""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from factory import policy
from factory.contracts import RELEASES_FILE_KEY, BugReport, RunState, Verdict
from factory.sre_base import alert_evidence, canonical_hash, evaluate_evidence_gate, tool_evidence
from factory import watcher as _watcher
from factory.watcher import Watcher, app_frames, iso, parse_ts, runs_dir, signature  # noqa: F401

CONFIRM_WAIT_S = 1.0   # how long to wait for the server to log a replayed request

# source_type per tool; graph metadata follows SRE-agent/agent/evidence_graph.py _graph_source_metadata
TOOL_SOURCE = {"server_log": "logs", "metrics_scrape": "metrics", "live_replay": "replay", "release_ledger": "releases"}


def _safe(method: str, path: str) -> bool:
    return method.upper() in policy.SAFE_REPLAY_METHODS and any(p.match(path) for p in policy.SAFE_REPLAY_PATHS)


def _log_confirm(watcher, route: str) -> Callable[[Any, dict], Optional[str]]:
    """Signature of the server's own log line for a replayed request.

    Tails the log with a private Watcher (no scrape targets) so the shared watcher's state is untouched.
    No matching line within CONFIRM_WAIT_S => None (not confirmed).
    """
    tail = Watcher(log_path=watcher.log_path)
    tail.remove_target("stable")
    tail.tick()
    tail.logs.clear()                             # skip lines already in the log

    def confirm(response, sample) -> Optional[str]:
        deadline = time.monotonic() + CONFIRM_WAIT_S
        while True:
            tail.tick()
            # ponytail: matches by path+status after the attempt began; concurrent identical traffic can match too
            rows = [e for e in tail.log_lines(since=sample["_sent_at"], instance="stable", route=route)
                    if e.get("path") == sample["path"] and e.get("status") == response.status_code]
            if rows:
                return signature(rows[-1])
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    return confirm


def _replay(client, sample: dict, sig: str, confirm) -> list[dict]:
    attempts = []
    for _ in range(policy.REPLAY_ATTEMPTS):
        sample["_sent_at"] = datetime.now(timezone.utc) - timedelta(milliseconds=50)
        try:
            resp = client.get(sample["path"], params=sample["query"] or None)
        except Exception as exc:  # unreachable server is missing evidence, not a failure
            attempts.append({"status": None, "signature": None, "error": f"{type(exc).__name__}: {exc}", "failed": False})
            continue
        got = confirm(resp, sample) if resp.status_code >= 500 else None
        attempts.append({"status": resp.status_code, "signature": got,
                         "failed": resp.status_code >= 500 and got == sig})
    sample.pop("_sent_at", None)
    return attempts


def _releases_summary(path: Path) -> dict:
    if not path.exists():
        return {"releases": 0, "summary": "no agent releases"}
    releases = json.loads(path.read_text()).get(RELEASES_FILE_KEY, [])
    if not releases:
        return {"releases": 0, "summary": "no agent releases"}
    last = releases[-1]
    return {"releases": len(releases), "latest_tag": last.get("tag"), "latest_status": last.get("status"),
            "summary": f"latest agent release {last.get('tag')} is {last.get('status')}"}


def _graph(ledger: list[dict]) -> list[dict]:
    """Ledger envelopes -> gate nodes (tool_evidence hard-codes source="mcp", so the type comes from the tool)."""
    nodes = []
    for e in ledger:
        alert = e["kind"] == "alert"
        nodes.append({
            "evidence_id": e["evidence_id"],
            "source_type": "alert" if alert else TOOL_SOURCE[e["tool"]],
            "source_name": e["source"] if alert else e["tool"],
            "summary": json.dumps(e["payload"], default=str)[:280],
            "freshness": e.get("freshness", "current"),
            "source_reliability": e.get("source_reliability", 0.95 if alert else 0.9),
        })
    return nodes


def run(state: RunState, *, watcher=None, http_client=None, base_url=None, replay_signature=None,
        releases_path=None, now=None) -> Verdict:
    alert = state.alert
    breach = alert.get("breach") or {}
    now = parse_ts(now) if now else datetime.now(timezone.utc)
    route = (alert.get("labels") or {}).get("path") or breach.get("route")
    started = breach.get("started_at") or alert.get("starts_at")
    if not route or not started:
        return Verdict("investigate", "unsure", ["alert has no route or start time"], {"alert_well_formed": False}, {})
    since = parse_ts(started) - timedelta(seconds=policy.ALERT_WINDOW_S)
    until = parse_ts(breach["detected_at"]) if breach.get("detected_at") else now
    watcher = watcher or _watcher.default()

    # 1-2. Group 5xx lines by signature.
    errors = [e for e in watcher.log_lines(since, until, instance="stable", route=route) if e.get("status", 0) >= 500]
    window = {"since": iso(since), "until": iso(until), "route": route}
    if not errors:
        return Verdict("investigate", "unsure", [f"no 5xx log lines for {route} in the alert window"],
                       {"errors_in_window": False}, {"window": window})
    counts = Counter(signature(e) for e in errors)
    sig, top = counts.most_common(1)[0]
    share = round(top / len(errors), 3)

    # 3. Sample request and safe replay.
    sample_line = [e for e in errors if signature(e) == sig][-1]
    method = sample_line.get("method", "GET")
    sample = {"method": method, "path": sample_line.get("path", route), "query": sample_line.get("query") or {},
              "body": None}
    safe = _safe(method, sample["path"])
    attempts: list[dict] = []
    if safe:
        own_client = http_client is None
        if own_client:
            import httpx
            http_client = httpx.Client(base_url=base_url or f"http://127.0.0.1:{policy.STABLE_PORT}", timeout=5)
        try:
            attempts = _replay(http_client, sample, sig, replay_signature or _log_confirm(watcher, route))
        finally:
            if own_client:
                http_client.close()
    failures = sum(a["failed"] for a in attempts)

    # 4. Frames -> files.
    files = list(dict.fromkeys(f for f, _ in app_frames(sample_line.get("traceback", ""))))

    # 5. Evidence ledger and graph.
    ctx = {"started_at": iso(since), "received_at": iso(until)}
    ledger = [alert_evidence({**alert, "source": "watcher", "raw_alert": alert, "started_at": started})]

    def add(tool: str, args: dict, observation: Any) -> str:
        ledger.append(tool_evidence(ledger=ledger, tool_name=tool, tool_args=args, observation=observation,
                                    incident_context=ctx))
        return ledger[-1]["evidence_id"]

    logs_id = add("server_log", window, {"signature_counts": dict(counts), "errors": len(errors),
                                         "dominant_signature": sig, "dominant_share": share, "sample_line": sample_line})
    cur, base = watcher.current(route), watcher.baseline(route)
    metrics_id = add("metrics_scrape", {"route": route, "instance": "stable"}, {"current": cur, "baseline": base})
    replay_id = add("live_replay", {**sample, "safe": safe}, {"attempts": attempts, "failures": failures})
    rel_path = Path(releases_path) if releases_path else runs_dir() / "releases.json"
    add("release_ledger", {"path": str(rel_path)}, _releases_summary(rel_path))
    graph = _graph(ledger)

    # 6. Candidate and gate.
    cur_rate = cur.get("metrics", {}).get("error_rate_percent")
    base_rate = base.get("metrics", {}).get("error_rate_percent", 0.0)
    metrics_elevated = (cur.get("samples", 0) > 0 and cur_rate is not None
                        and cur_rate >= policy.ALERT_ERROR_RATE_PERCENT and cur_rate > base_rate)
    citations = [logs_id] + ([metrics_id] if metrics_elevated else []) + ([replay_id] if failures else [])
    contradictions = []
    if not attempts or failures == 0:
        contradictions.append("critical: replay did not reproduce")
    elif failures < policy.REPLAY_REQUIRED_FAILURES:
        contradictions.append(f"critical: replay reproduced only {failures}/{policy.REPLAY_ATTEMPTS}")
    if not files:
        contradictions.append(f"critical: no frames under {policy.APP_DIR}")
    candidate = {"score": share, "citations": citations, "contradicting_signals": contradictions, "signature": sig}
    gate = evaluate_evidence_gate([candidate], graph, minimum_confidence=policy.MIN_SIGNATURE_SHARE)

    # 7. Extra checks (PRD 7.5).
    by_id = {n["evidence_id"]: n for n in graph}
    checks = {
        "dominant_share": share >= policy.MIN_SIGNATURE_SHARE,
        "replay_safe": safe,
        "replay_reproduced": failures == policy.REPLAY_REQUIRED_FAILURES,
        "source_types": len({by_id[c]["source_type"] for c in citations}) >= policy.MIN_SOURCE_TYPES,
        "files_in_app": bool(files) and all(f.startswith(policy.APP_DIR) and ".." not in f for f in files),
        "evidence_gate": gate["passed"],
    }
    reasons = []
    if not checks["dominant_share"]:
        reasons.append(f"dominant signature share {share} < {policy.MIN_SIGNATURE_SHARE}")
    if not safe:
        reasons.append("request not safe to replay")
    elif not checks["replay_reproduced"]:
        reasons.append(f"replay reproduced {failures}/{policy.REPLAY_ATTEMPTS} with the same signature")
    if not checks["source_types"]:
        reasons.append(f"fewer than {policy.MIN_SOURCE_TYPES} independent source types")
    if not checks["files_in_app"]:
        reasons.append(f"frames do not resolve to files under {policy.APP_DIR}")
    if not gate["passed"]:
        reasons.append(f"evidence gate failed: {sorted(k for k, v in gate['checks'].items() if not v)}")

    # 8. Bug report and artifact.
    bug = BugReport(
        id=f"BUG-{canonical_hash(sig)[:8].upper()}", alert_id=alert.get("incident_id", ""), signature=sig,
        exception_type=sample_line.get("exception_type") or f"HTTP{sample_line.get('status')}",
        traceback=sample_line.get("traceback", ""), sample_request=sample, files=files, error_share=share,
        replay_failures=failures, evidence_ids=[e["evidence_id"] for e in ledger])
    status = "pass" if all(checks.values()) else "unsure"
    if status == "pass":
        state.bug = bug
    content = {"ledger": ledger, "graph": graph, "candidate": candidate, "gate": gate, "checks": checks,
               "bug_report": asdict(bug)}
    out = Path(state.artifacts_dir) / "investigation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(content, indent=2, default=str))
    (out.parent / "bug.json").write_text(json.dumps(asdict(bug), indent=2, sort_keys=True))   # read by replay
    evidence = {"investigation_path": str(out), "investigation_sha256": canonical_hash(json.loads(out.read_text())),
                "bug_report_sha256": canonical_hash(asdict(bug)),
                "gate": gate, "signature_counts": dict(counts), "replay_attempts": attempts,
                "bug_report": asdict(bug), "window": window}
    return Verdict("investigate", status, reasons or [f"{sig}: share {share}, replay {failures}/{policy.REPLAY_ATTEMPTS}"],
                   checks, evidence)
