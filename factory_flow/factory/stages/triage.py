"""Stage 2: own release in watch window? (PRD A6, §7.6). The agent rolls back only its own releases."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from factory import policy, workspace
from factory.contracts import RELEASES_FILE_KEY, Release, RunState, Verdict

def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _git_prod_head() -> tuple[str, str]:
    out = subprocess.run(["git", "-C", str(workspace.target_dir()), "log", "-1", "--format=%H %cI", "prod"],
                         capture_output=True, text=True, check=True, timeout=10).stdout.split()
    return out[0], out[1]


def _release_tags(sha: str) -> list[str]:
    """release-* tags on commit sha (release-0 comes from init_repo.sh, not the ledger); [] if unknown."""
    proc = subprocess.run(["git", "-C", str(workspace.target_dir()), "tag", "--points-at", sha, "--list", "release-*"],
                          capture_output=True, text=True, timeout=10)
    return proc.stdout.split() if proc.returncode == 0 else []


def run(state: RunState, *, now: Optional[datetime] = None, releases_path: Optional[Path] = None,
        prod_head: Optional[Callable[[], tuple[str, str]]] = None) -> Verdict:
    now = now or datetime.now(timezone.utc)
    alert_at = _parse(state.alert["starts_at"]) if state.alert.get("starts_at") else now
    path = Path(releases_path) if releases_path else workspace.runs_dir() / "releases.json"
    releases = json.loads(path.read_text())[RELEASES_FILE_KEY] if path.exists() else []
    evidence: dict = {"releases_path": str(path), "releases": len(releases), "alert_at": alert_at.isoformat()}

    def recent(ts: str) -> bool:
        return abs((alert_at - _parse(ts)).total_seconds()) <= policy.ROLLBACK_LOOKBACK_S

    for rec in reversed(releases):  # newest first
        if rec["status"] == "watching" or (rec["status"] != "rolled_back" and recent(rec["promoted_at"])):
            state.release = Release(**rec)
            evidence["release_tag"] = rec["tag"]
            return Verdict("triage", "fail", [f"own release {rec['tag']} in watch window"],
                           {"no_own_release": False}, evidence, route="rollback")

    try:
        sha, committed_at = (prod_head or _git_prod_head)()
    except Exception as exc:  # missing evidence is unsure, never pass (CLAUDE.md)
        evidence["prod_head_error"] = f"{type(exc).__name__}: {exc}"
        return Verdict("triage", "unsure", ["prod head unknown: cannot rule out a human release"],
                       {"no_own_release": True, "prod_head_known": False}, evidence)
    evidence.update(prod_head=sha, prod_committed_at=committed_at)
    human = sha not in {r["commit"] for r in releases} and recent(committed_at)
    if human and (tags := _release_tags(sha)):
        human, evidence["prod_release_tags"] = False, tags
    checks = {"no_own_release": True, "no_recent_human_release": not human}
    if human:
        return Verdict("triage", "unsure", ["human release, not reverted"], checks, evidence)
    return Verdict("triage", "pass", ["no recent release"], checks, evidence)
