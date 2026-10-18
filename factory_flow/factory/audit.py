"""Hash-chained, HMAC-signed JSONL audit log (PRD A2, entry format PRD 7.7).

Field naming follows SRE-agent/runtime/storage.py log_audit_event (run_id, actor, payload fields inline);
the chain uses sre_base.canonical_hash (SRE-agent/agent/approval.py).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from factory.sre_base import canonical_hash

FACTORY_FLOW_DIR = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()  # ponytail: in-process lock only; one agent process writes the log


def runs_dir() -> Path:
    """runs/ directory (FACTORY_RUNS_DIR overrides), created if missing."""
    path = Path(os.environ.get("FACTORY_RUNS_DIR", FACTORY_FLOW_DIR / "runs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_path() -> Path:
    return runs_dir() / "audit.jsonl"


def _key() -> bytes:
    key = os.environ.get("AUDIT_HMAC_KEY")
    if not key:
        raise RuntimeError("AUDIT_HMAC_KEY is not set; refusing to write or verify an unsigned audit log")
    return key.encode()


def _sign(entry_hash: str, key: bytes) -> str:
    return hmac.new(key, entry_hash.encode(), hashlib.sha256).hexdigest()


def _entry_hash(entry: dict) -> str:
    return canonical_hash({k: v for k, v in entry.items() if k not in ("entry_hash", "hmac")})


def _last_entry(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else None  # ponytail: reads whole file per append; fine at demo size


def append(event: dict) -> dict:
    """Add seq, ts, prev_hash, entry_hash, hmac; append one line to runs/audit.jsonl; return the entry."""
    key = _key()
    # JSON round trip first so the hash covers exactly what is written (Paths, tuples, int keys).
    entry = json.loads(json.dumps(event, default=str))
    entry.setdefault("actor", "agent")
    with _LOCK:
        path = audit_path()
        last = _last_entry(path)
        entry["seq"] = (last["seq"] + 1) if last else 1
        entry["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry["prev_hash"] = last["entry_hash"] if last else ""
        entry["entry_hash"] = _entry_hash(entry)
        entry["hmac"] = _sign(entry["entry_hash"], key)
        with path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def verify(path: Optional[Path] = None) -> tuple[bool, Optional[int]]:
    """(ok, first_bad_seq). Checks seq order, prev_hash chain, entry_hash and HMAC of every line."""
    key = _key()
    path = Path(path) if path else audit_path()
    if not path.exists():
        return True, None
    prev_hash = ""
    for position, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            entry = json.loads(line)
            ok = (
                entry["seq"] == position
                and entry["prev_hash"] == prev_hash
                and entry["entry_hash"] == _entry_hash(entry)
                and hmac.compare_digest(entry["hmac"], _sign(entry["entry_hash"], key))
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            return False, position
        prev_hash = entry["entry_hash"]
    return True, None


def read(run_id: Optional[str] = None) -> list[dict]:
    """All entries, or only those of run_id. Does not verify; call verify() for that."""
    path = audit_path()
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in entries if run_id is None or e.get("run_id") == run_id]
