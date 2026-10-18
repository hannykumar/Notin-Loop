from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4
from integrations.incident_registry import validate_incident_id


ARTIFACTS_ROOT = Path("artifacts")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_shell(path: Path, commands: list[str]) -> None:
    lines = ["#!/usr/bin/env bash", "set -e"] + commands
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_run_artifacts(result: Dict[str, Any]) -> Path:
    incident_id = validate_incident_id(str(result.get("incident_id", "unknown")))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{ts}_{uuid4().hex[:8]}"
    out_dir = ARTIFACTS_ROOT / incident_id / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_json(out_dir / "summary.json", result)
    _write_json(out_dir / "plan.json", result.get("plan", []))
    _write_json(out_dir / "sanitized_plan.json", result.get("sanitized_plan", []))
    _write_json(out_dir / "execution_results.json", result.get("execution_results", []))
    _write_json(out_dir / "policy_violations.json", result.get("policy_violations", []))

    _write_shell(out_dir / "executed_commands.sh", result.get("planned_commands", []) or ["# no commands"])
    _write_shell(out_dir / "rollback_commands.sh", result.get("rollback_commands", []) or ["# no rollback"])

    rca = str(result.get("rca_final") or result.get("rca_draft") or "")
    (out_dir / "rca.md").write_text(rca, encoding="utf-8")

    trace_ref = str(result.get("saved_trace", ""))
    (out_dir / "trace_ref.txt").write_text(trace_ref + "\n", encoding="utf-8")
    return out_dir
