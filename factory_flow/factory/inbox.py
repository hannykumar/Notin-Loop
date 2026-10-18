"""Escalation cases with evidence (PRD A3).

Replaces the human approve-execute step of SRE-agent/runtime/api.py: instead of waiting for approval,
every stopped run lands here as one JSON case for a human to read.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from factory.audit import runs_dir
from factory.contracts import RunState, Verdict


def _inbox_dir() -> Path:
    path = runs_dir() / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def escalate(state: RunState, verdict: Verdict, severity: Literal["normal", "critical"] = "normal") -> Path:
    """Write runs/inbox/<run_id>.json for the stopping verdict and return its path."""
    verdicts = [asdict(v) for v in state.verdicts]
    case = {
        "run_id": state.run_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    path = _inbox_dir() / f"{state.run_id}.json"
    path.write_text(json.dumps(case, indent=2, default=str))
    return path


def list_cases() -> list[dict]:
    """All cases, oldest first."""
    cases = [json.loads(p.read_text()) for p in _inbox_dir().glob("*.json")]
    return sorted(cases, key=lambda c: c["created_at"])


def get(run_id: str) -> Optional[dict]:
    path = _inbox_dir() / f"{run_id}.json"
    return json.loads(path.read_text()) if path.exists() else None
