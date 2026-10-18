# STUB(track A): replace with factory.inbox (A3) at T1
"""Minimal escalation inbox for B6 until Track A's factory/inbox.py (A3) lands.

open_case(kind, severity, run_id, summary, evidence) -> Path writes one JSON case to
runs/inbox/<case_id>.json (atomic temp file + replace) and returns its path. A3's inbox must accept the
same fields (or rollback.py switches its import at T1).

Case shape: {"case_id", "kind", "severity", "run_id", "summary", "status": "open", "created_at", "evidence"}.

Patterned on the approve-execute flow in SRE-agent/runtime/api.py @ b4f7e0c (a stored record a human acts
on); new code otherwise.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from factory import workspace

INBOX_DIR = workspace.RUNS_DIR / "inbox"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def open_case(kind: str, severity: str, run_id: str, summary: str, evidence: dict[str, Any]) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    case_id = _SAFE.sub("-", f"{stamp}-{kind}-{run_id or 'manual'}-{uuid.uuid4().hex[:6]}")
    case = {
        "case_id": case_id,
        "kind": kind,
        "severity": severity,
        "run_id": run_id,
        "summary": summary,
        "status": "open",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence": evidence,
    }
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = INBOX_DIR / f"{case_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(case, indent=2, sort_keys=True, default=str))
    tmp.replace(path)
    return path


def list_cases() -> list[dict[str, Any]]:
    if not INBOX_DIR.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(INBOX_DIR.glob("*.json"))]
