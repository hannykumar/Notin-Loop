from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_proposal_artifact(run_id: str, plan_result: Dict[str, Any], *, ttl_minutes: int = 30) -> Dict[str, Any]:
    action = dict(plan_result.get("proposed_action") or {})
    context = dict(plan_result.get("incident_context") or {})
    options = list(plan_result.get("remediation_options") or [])
    recommended = next((dict(item) for item in options if item.get("recommended")), {})
    created_at = datetime.now(timezone.utc)
    proposal = {
        "artifact_version": "1",
        "proposal_id": f"proposal_{uuid4().hex[:12]}",
        "run_id": run_id,
        "diagnosis": str(plan_result.get("diagnosis") or "Unknown"),
        "evidence_snapshot_hash": canonical_hash(plan_result.get("evidence_ledger") or plan_result.get("evidence") or {}),
        "action": action,
        "service": str(context.get("service") or action.get("target") or ""),
        "namespace": str(context.get("namespace") or action.get("namespace") or "default"),
        "environment": str(context.get("environment") or "unknown"),
        "expected_effect": str(recommended.get("expected_effect") or action.get("reason") or ""),
        "risk": str(recommended.get("risk") or "unknown"),
        "verification_plan": list(recommended.get("verification_plan") or []),
        "rollback_plan": list(recommended.get("rollback_plan") or plan_result.get("rollback_commands") or []),
        "created_at": created_at.isoformat(),
        "expires_at": (created_at + timedelta(minutes=ttl_minutes)).isoformat(),
    }
    proposal["proposal_hash"] = canonical_hash(proposal)
    return proposal


def verify_proposal_artifact(artifact: Dict[str, Any], plan_result: Dict[str, Any]) -> None:
    if not artifact or str(artifact.get("artifact_version")) != "1":
        raise ValueError("Approval proposal artifact is missing or unsupported")
    supplied_hash = str(artifact.get("proposal_hash") or "")
    unhashed = {key: value for key, value in artifact.items() if key != "proposal_hash"}
    if not supplied_hash or canonical_hash(unhashed) != supplied_hash:
        raise ValueError("Approval proposal hash does not match the stored proposal")
    if dict(artifact.get("action") or {}) != dict(plan_result.get("proposed_action") or {}):
        raise ValueError("Approved action does not match the planned action")
    expected_evidence_hash = canonical_hash(plan_result.get("evidence_ledger") or plan_result.get("evidence") or {})
    if str(artifact.get("evidence_snapshot_hash") or "") != expected_evidence_hash:
        raise ValueError("Approved evidence snapshot no longer matches the plan")
    expires_at = datetime.fromisoformat(str(artifact.get("expires_at")).replace("Z", "+00:00"))
    if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
        raise ValueError("Approval proposal has expired")
