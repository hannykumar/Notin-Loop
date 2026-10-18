"""Escalation cases with evidence (PRD A3).

Replaces the human approve-execute step of SRE-agent/runtime/api.py: instead of waiting for approval,
every stopped run lands here as one JSON case for a human to read.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from factory.audit import runs_dir
from factory.contracts import RunState, Verdict


def _inbox_dir() -> Path:
    path = runs_dir() / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


SEVERITY_RANK = {"normal": 0, "high": 1, "critical": 2}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(run_id: str, case: dict) -> Path:
    """One case per run: merge into runs/inbox/<run_id>.json (evidence merged, highest severity kept)."""
    path = _inbox_dir() / f"{run_id}.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    merged = {**old, **case, "evidence": {**old.get("evidence", {}), **case.get("evidence", {})}}
    merged["created_at"] = old.get("created_at", case["created_at"])
    merged["severity"] = max(old.get("severity", "normal"), case["severity"], key=lambda s: SEVERITY_RANK.get(s, 0))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2, default=str))
    tmp.replace(path)
    return path


def open_case(kind: str, severity: str, run_id: str, summary: str, evidence: dict[str, Any]) -> Path:
    """A case raised by a stage itself (rollback); the runner's escalate() later merges into the same file."""
    return _write(run_id, {"run_id": run_id, "created_at": _now(), "kind": kind, "severity": severity,
                           "summary": summary, "evidence": evidence})


def escalate(state: RunState, verdict: Verdict, severity: Literal["normal", "high", "critical"] = "normal") -> Path:
    """Write runs/inbox/<run_id>.json for the stopping verdict and return its path."""
    verdicts = [asdict(v) for v in state.verdicts]
    case = {
        "run_id": state.run_id,
        "created_at": _now(),
        "severity": severity,
        "stage": verdict.stage,
        "status": verdict.status,
        "reason": "; ".join(verdict.reasons) or f"{verdict.stage} returned {verdict.status}",
        "alert": state.alert,
        "bug": asdict(state.bug) if state.bug else None,
        "verdicts": verdicts,
        "stopping_verdict": asdict(verdict),
        "evidence": {
            "artifacts_dir": str(state.artifacts_dir),
            **{v["stage"]: v["evidence"] for v in verdicts + [asdict(verdict)] if v["evidence"]},
        },
    }
    return _write(state.run_id, case)


def list_cases() -> list[dict]:
    """All cases, oldest first."""
    cases = [json.loads(p.read_text()) for p in _inbox_dir().glob("*.json")]
    return sorted(cases, key=lambda c: c["created_at"])


def get(run_id: str) -> Optional[dict]:
    path = _inbox_dir() / f"{run_id}.json"
    return json.loads(path.read_text()) if path.exists() else None
