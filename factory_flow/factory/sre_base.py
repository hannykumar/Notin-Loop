"""The only bridge to SRE-agent (PRD 5.2). No other factory module imports SRE-agent directly."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

FACTORY_FLOW_DIR = Path(__file__).resolve().parents[1]
# SRE-agent is vendored as a plain copy (no .git); this is the upstream commit it was copied from.
SRE_AGENT_BASE_COMMIT = "b4f7e0cc26fe879c2eb74a53691d84a6bc20685d"


def _load_dotenv() -> None:
    env_file = FACTORY_FLOW_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
SRE_AGENT_PATH = (FACTORY_FLOW_DIR / os.environ.get("SRE_AGENT_PATH", "../SRE-agent")).resolve()
if not (SRE_AGENT_PATH / "agent" / "approval.py").exists():
    raise RuntimeError(f"SRE-agent not found at {SRE_AGENT_PATH}; set SRE_AGENT_PATH in factory_flow/.env")
if str(SRE_AGENT_PATH) not in sys.path:
    sys.path.insert(0, str(SRE_AGENT_PATH))

# Stdlib-only SRE-agent modules: safe to import without SRE-agent's dependencies.
from agent.approval import canonical_hash  # noqa: E402
from agent.evidence_ledger import alert_evidence, evaluate_evidence_gate, tool_evidence  # noqa: E402
from runtime.alert_sources import normalize_generic_alert  # noqa: E402


def sre_agent_commit() -> str:
    """Commit of the SRE-agent base; recorded in every run's first audit entry."""
    try:
        return subprocess.run(
            ["git", "-C", str(SRE_AGENT_PATH), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return SRE_AGENT_BASE_COMMIT


# Adapted from SRE-agent/executor/engine.py @ b4f7e0c (ported: engine.py imports Kubernetes/GitOps/settings).
# Note: metrics are read from before["metrics"] / after["metrics"], not top-level keys.
def classify_verification_outcome(
    before: Dict[str, Any],
    after: Dict[str, Any],
    rule_verification: Dict[str, Any],
    *,
    timed_out: bool = False,
) -> str:
    if timed_out:
        return "verification_timeout"
    if rule_verification.get("resolved"):
        return "resolved"

    before_metrics = dict(before.get("metrics") or {})
    after_metrics = dict(after.get("metrics") or {})
    comparable = 0
    improved = 0
    regressed = 0
    for key in ["error_rate_percent", "dns_error_rate_percent", "memory_percent", "p95_latency_ms"]:
        before_value = before_metrics.get(key)
        after_value = after_metrics.get(key)
        if not isinstance(before_value, (int, float)) or not isinstance(after_value, (int, float)):
            continue
        comparable += 1
        tolerance = max(abs(float(before_value)) * 0.1, 0.1)
        delta = float(after_value) - float(before_value)
        if delta > tolerance:
            regressed += 1
        elif delta < -tolerance:
            improved += 1

    if regressed and regressed >= improved:
        return "regressed"
    if improved:
        return "improved"
    if comparable:
        return "unchanged"
    return "inconclusive"


__all__ = [
    "SRE_AGENT_PATH",
    "alert_evidence",
    "canonical_hash",
    "classify_verification_outcome",
    "evaluate_evidence_gate",
    "normalize_generic_alert",
    "sre_agent_commit",
    "tool_evidence",
]
