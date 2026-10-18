from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List


MOCK_INCIDENTS_DIR = Path("lab/fixtures/incidents")
INCIDENT_ID_PATTERN = re.compile(r"^INC-(\d+)$")
DEFAULT_ACTIVE_INCIDENT_IDS = ("INC-001", "INC-002", "INC-003", "INC-004", "INC-007")


def validate_incident_id(incident_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", incident_id):
        raise ValueError("Incident identifier must use letters, numbers, dots, underscores or hyphens")
    return incident_id


def active_incident_ids() -> set[str]:
    raw = os.getenv("SRE_ACTIVE_INCIDENT_IDS", "").strip()
    if raw:
        return {item.strip().upper() for item in raw.split(",") if item.strip()}
    return set(DEFAULT_ACTIVE_INCIDENT_IDS)


def _sort_key(incident_id: str) -> tuple[int, str]:
    match = INCIDENT_ID_PATTERN.match(str(incident_id).strip().upper())
    if match:
        return int(match.group(1)), str(incident_id)
    return 9999, str(incident_id)


def list_mock_incidents() -> List[Dict[str, Any]]:
    incidents: List[Dict[str, Any]] = []
    active_ids = active_incident_ids()
    for path in sorted(MOCK_INCIDENTS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        incident_id = str(payload.get("incident_id", "")).strip().upper()
        if not incident_id:
            continue
        if active_ids and incident_id not in active_ids:
            continue
        incidents.append(
            {
                "incident_id": incident_id,
                "title": str(payload.get("title", "")).strip() or incident_id,
                "description": str(payload.get("description", "")).strip(),
                "service": str(payload.get("service", "")).strip(),
                "namespace": str(payload.get("namespace", "")).strip() or "prod",
                "expected_incident_type": str(
                    payload.get("expected_incident_type") or payload.get("incident_type") or ""
                ).strip(),
                "path": str(path),
            }
        )
    incidents.sort(key=lambda item: _sort_key(str(item.get("incident_id", ""))))
    return incidents


def incident_ids() -> List[str]:
    ids = [str(item.get("incident_id", "")).strip().upper() for item in list_mock_incidents()]
    return [item for item in ids if item]


def incident_exists(incident_id: str) -> bool:
    target = str(incident_id or "").strip().upper()
    return any(item == target for item in incident_ids())
