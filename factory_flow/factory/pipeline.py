"""Stage runner with kill switch, audit and escalation (PRD A3, workflow PRD 6).

Patterned on SRE-agent/runtime/job_runner.py: a run executes a fixed stage list (no replanning),
logs an audit event per step and turns exceptions into a recorded failure instead of crashing.

One deps dict serves the whole run; each stage gets only the keys its run() signature names:
    deps = {
        "watcher": watcher.default(),       # investigate, canary, promote, watch, rollback (default if omitted)
        "workspace": factory.workspace,     # qualify, reproduce, fix, inspect_diff (default if omitted)
        "fixer": <Fixer>,                   # fix (required)
        "http_client": httpx.Client(base_url="http://127.0.0.1:8101"),   # investigate replay (optional)
        "releases_path": releases.ledger_path(),                         # triage, investigate (optional)
    }
Paths come from FACTORY_RUNS_DIR / FACTORY_TARGET_SERVICE at call time.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import secrets
from pathlib import Path
from typing import Callable, Optional

from factory import audit, inbox
from factory.audit import runs_dir
from factory.contracts import RunState, Verdict
from factory.sre_base import sre_agent_commit

StageFn = Callable[[RunState], Verdict]
STAGE_ORDER = ["triage", "investigate", "qualify", "reproduce", "fix", "inspect_diff", "oracle", "canary", "promote",
               "watch"]
# stage name -> (module, function); default factory.stages.<name>.run
_TARGETS = {"fix": ("factory.fixers", "run_fix"), "watch": ("factory.stages.promote", "watch_stage")}
POLICY_FILE = Path(__file__).with_name("policy.py")


def _unsure(stage: str, reason: str, **checks: bool) -> Verdict:
    return Verdict(stage=stage, status="unsure", reasons=[reason], checks=checks or {"completed": False}, evidence={})


def resolve(name: str, deps: Optional[dict] = None) -> StageFn:
    """Lazy stage: imports factory.stages.<name>.run (or the _TARGETS entry) at call time.

    Passes only the deps the target's signature names (after the state argument).
    A missing module or function yields "unsure: stage not implemented", never a pass.
    """
    deps = deps or {}
    module_name, attr = _TARGETS.get(name, (f"factory.stages.{name}", "run"))

    def stage(state: RunState) -> Verdict:
        try:
            fn = getattr(importlib.import_module(module_name), attr, None)
        except ModuleNotFoundError:
            fn = None
        if fn is None:
            return _unsure(name, "stage not implemented", implemented=False)
        params = list(inspect.signature(fn).parameters)[1:]
        return fn(state, **{k: v for k, v in deps.items() if k in params})

    return stage


DEFAULT_STAGES: list[tuple[str, StageFn]] = [(n, resolve(n)) for n in STAGE_ORDER + ["rollback"]]


# --- Kill switch -----------------------------------------------------------------

def _paused_file() -> Path:
    return runs_dir() / "PAUSED"


def is_paused() -> bool:
    return _paused_file().exists()


def pause(actor: str = "human", reason: str = "") -> None:
    _paused_file().write_text(reason or "paused")
    audit.append({"run_id": "", "stage": "pause", "actor": actor, "reasons": [reason] if reason else []})


def resume(actor: str = "human") -> None:
    _paused_file().unlink(missing_ok=True)
    audit.append({"run_id": "", "stage": "resume", "actor": actor, "reasons": []})


# --- Runner ----------------------------------------------------------------------

def _execute(state: RunState, name: str, fn: StageFn) -> Verdict:
    """Run one stage fail-safe, record and audit its verdict."""
    try:
        verdict = fn(state)
        if not isinstance(verdict, Verdict):
            verdict = _unsure(name, f"stage returned {type(verdict).__name__}, not a Verdict")
        elif verdict.status == "pass" and not all(verdict.checks.values()):
            verdict = _unsure(name, f"status pass but failed checks: {verdict.checks}", consistent=False)
    except Exception as exc:  # any crash is missing evidence => unsure
        verdict = _unsure(name, f"exception {type(exc).__name__}: {exc}", completed=False)
    state.verdicts.append(verdict)
    audit.append({
        "run_id": state.run_id, "stage": verdict.stage, "actor": "agent", "status": verdict.status,
        "reasons": verdict.reasons, "checks": verdict.checks, "evidence": verdict.evidence, "route": verdict.route,
    })
    return verdict


def _paused_stop(state: RunState, next_stage: str) -> bool:
    if not is_paused():
        return False
    audit.append({"run_id": state.run_id, "stage": "paused", "actor": "agent", "status": "unsure",
                  "reasons": [f"runs/PAUSED present before {next_stage}"]})
    inbox.escalate(state, _unsure(next_stage, "line paused (runs/PAUSED) before this stage", not_paused=False))
    return True


def _run_line(state: RunState, stages: list[tuple[str, StageFn]], deps: Optional[dict]) -> str:
    rollback = dict(stages).get("rollback") or resolve("rollback", deps)
    for name, fn in stages:
        if name == "rollback":  # reached only via route == "rollback"
            continue
        if _paused_stop(state, name):
            return "paused"
        verdict = _execute(state, name, fn)
        if verdict.route == "rollback":
            if _paused_stop(state, "rollback"):
                return "paused"
            rb = _execute(state, "rollback", rollback)
            if rb.status == "pass":
                inbox.escalate(state, verdict)
                return "rolled_back"
            pause(actor="agent", reason=f"rollback {rb.status} in {state.run_id}")  # PRD 6: pause line, critical
            inbox.escalate(state, rb, severity="critical")
            return "escalated"
        if verdict.status != "pass":
            inbox.escalate(state, verdict)
            return "escalated"
    return "kept"


def run(alert: dict, *, stages: Optional[list[tuple[str, StageFn]]] = None, deps: Optional[dict] = None) -> RunState:
    """Run the line for one normalised alert. Only an unbroken chain of pass ends in "kept"."""
    if stages is None:
        stages = [(n, resolve(n, deps)) for n in STAGE_ORDER + ["rollback"]]
    run_id = f"run_{secrets.token_hex(4)}"
    artifacts_dir = runs_dir() / "artifacts" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    state = RunState(run_id=run_id, alert=alert, artifacts_dir=artifacts_dir)
    audit.append({
        "run_id": run_id, "stage": "run_start", "actor": "agent", "alert": alert,
        "sre_agent_commit": sre_agent_commit(),
        "policy_sha256": hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest(),
    })
    outcome = _run_line(state, stages, deps)
    audit.append({"run_id": run_id, "stage": "run_end", "actor": "agent", "outcome": outcome})
    return state
