"""Stage 5: test from the real failing request (PRD A9). The LLM writes the test; code decides."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from factory import llm, policy, sre_base
from factory.contracts import BugReport, RunState, Verdict

STAGE = "reproduce"
# Prompt style patterned on SRE-agent/agent/prompts.py (rules list, "return exactly ... and nothing else").
REPRO_SYSTEM = """
You write one pytest file that reproduces a production bug in a FastAPI service.

Rules:
- Return exactly one Python file and nothing else.
- Use `from fastapi.testclient import TestClient` and `from app.main import app`.
- Build the client as `TestClient(app)` (raise_server_exceptions=True, the default) so the original exception surfaces.
- Replay exactly the sample request: same method, path, query parameters and body.
- Assert the response is not a server error, e.g. `assert response.status_code < 500`.
  On the buggy code the test must fail; once the bug is fixed it must pass.
- No skips, xfail, pytest.fail, `assert False`, sleeps, network, subprocess, file writes or other HTTP clients.
""".strip()

_BANNED_MODULES = {"requests", "httpx", "socket", "subprocess", "urllib"}
_BANNED_ATTRS = {"fail", "skip", "skipif", "xfail", "importorskip", "sleep", "system", "write_text", "write_bytes"}
_BANNED_CALLS = {"open", "exec", "eval", "__import__"}


def lint(text: str) -> str:
    """Return a rejection reason, or "" if the test is acceptable."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return f"syntax error: {exc}"
    imports: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.update((node.module or "", a.name) for a in node.names)
            mods = {node.module or ""}
        elif isinstance(node, ast.Import):
            mods = {a.name for a in node.names}
        else:
            mods = set()
        if bad := {m for m in mods if m.split(".")[0] in _BANNED_MODULES}:
            return f"banned import {sorted(bad)[0]}"
        if isinstance(node, ast.Attribute) and node.attr in _BANNED_ATTRS:
            return f"banned use of .{node.attr}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
            return f"banned call {node.func.id}()"
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and not node.test.value:
            return "assert on a constant false value"
    if ("fastapi.testclient", "TestClient") not in imports:
        return "missing `from fastapi.testclient import TestClient`"
    if ("app.main", "app") not in imports:
        return "missing `from app.main import app`"
    if not any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_") for n in ast.walk(tree)):
        return "no test function"
    return ""


def _summary(result: dict) -> dict:
    return {k: result[k] for k in ("passed", "failed", "errors", "skipped", "exit_code")} | {
        "failure_types": [f["type"] for f in result["failures"]]}


def _validate(workspace, bug: BugReport, name: str, text: str) -> tuple[str, list[dict]]:
    """Run the test REPRO_RUNS times on fresh prod; return (rejection reason or "", run summaries)."""
    allowed = {bug.exception_type, "AssertionError"}
    tree = workspace.fresh_worktree("prod")
    (tree / "tests").mkdir(exist_ok=True)
    (tree / "tests" / name).write_text(text)
    runs = []
    for i in range(policy.REPRO_RUNS):
        result = workspace.run_pytest(tree, f"tests/{name}")
        runs.append(_summary(result))
        if result["errors"] or result["passed"] or result["failed"] < 1:
            return (f"run {i + 1} not a valid red: passed={result['passed']} failed={result['failed']} "
                    f"errors={result['errors']} {runs[-1]['failure_types']}"), runs
        if bad := [t for t in runs[-1]["failure_types"] if t not in allowed]:
            return f"run {i + 1} failed for the wrong reason: {bad}", runs
    return "", runs


def _prompt(bug: BugReport, sources: str, feedback: str) -> str:
    req = bug.sample_request
    user = (
        f"Signature: {bug.signature}\nException: {bug.exception_type}\nTraceback:\n{bug.traceback}\n\n"
        f"Sample request: method={req.get('method', 'GET')} path={req.get('path')} "
        f"query={req.get('query') or {}} body={req.get('body')}\n\nSource files:\n{sources}"
    )
    if feedback:
        user += f"\n\nYour previous test was rejected: {feedback}\nWrite a corrected test."
    return user


def _workspace_module():
    try:  # STUB(track B): factory/workspace.py (B2) may not be ready; used as a module exposing the Workspace API
        from factory import workspace as ws_module
    except ImportError:
        return None
    return ws_module if hasattr(ws_module, "fresh_worktree") and hasattr(ws_module, "run_pytest") else None


def run(state: RunState, *, workspace=None, fallback_dir=None, llm_complete=None) -> Verdict:
    evidence: dict = {"attempts": 0, "fallback_used": False, "rejections": []}
    checks = {"bug_report_present": False, "test_written": False, "lint_ok": False, "red_3_of_3": False}

    def verdict(status: str, reason: str) -> Verdict:
        evidence["llm"] = llm.status()
        return Verdict(stage=STAGE, status=status, reasons=[reason], checks=checks, evidence=evidence)

    bug = state.bug
    if bug is None:
        return verdict("unsure", "no bug report")
    checks["bug_report_present"] = True
    workspace = workspace or _workspace_module()
    if workspace is None:
        return verdict("unsure", "workspace unavailable")
    complete = llm_complete or llm.complete
    name = f"test_repro_{re.sub(r'[^a-z0-9]', '_', bug.id.lower())}.py"
    target = Path(os.environ.get("FACTORY_TARGET_SERVICE", sre_base.FACTORY_FLOW_DIR / "target_service"))
    fallback = Path(fallback_dir or target / "fallback_tests") / f"test_repro_{Path(bug.files[0]).stem}.py" \
        if bug.files else None

    try:
        tree = workspace.fresh_worktree("prod")
        sources = "\n\n".join(f"### {f}\n{(tree / f).read_text()}" for f in bug.files if (tree / f).is_file())
        text, reason, runs = "", "", []
        for attempt in range(1, policy.REPRO_ATTEMPTS + 1):
            try:
                text = llm.strip_fences(complete(REPRO_SYSTEM, _prompt(bug, sources, reason)))
                evidence["attempts"] = attempt
            except llm.LLMUnavailable as exc:
                evidence["llm_error"] = str(exc)
                if not (fallback and fallback.is_file()):
                    return verdict("unsure", f"LLM unavailable and no fallback test at {fallback}")
                text, evidence["fallback_used"], evidence["fallback_path"] = fallback.read_text(), True, str(fallback)
            checks["test_written"] = True
            reason = lint(text)
            if not reason:
                checks["lint_ok"] = True
                reason, runs = _validate(workspace, bug, name, text)
                evidence["runs"] = runs
            if not reason:
                break
            evidence["rejections"].append(reason)
            if evidence["fallback_used"]:
                return verdict("unsure", f"fallback test rejected: {reason}")
        else:
            return verdict("unsure", f"no valid reproduction after {policy.REPRO_ATTEMPTS} attempts: {reason}")
    finally:
        if hasattr(workspace, "cleanup"):
            workspace.cleanup()

    checks["red_3_of_3"] = True
    state.artifacts_dir.mkdir(parents=True, exist_ok=True)
    frozen = state.artifacts_dir / name
    frozen.write_text(text)
    state.failing_test = frozen
    state.test_sha256 = sre_base.canonical_hash(frozen.read_text())
    evidence.update(test_path=str(frozen), test_sha256=state.test_sha256)
    return verdict("pass", f"test fails {policy.REPRO_RUNS}/{policy.REPRO_RUNS} with {sorted({t for r in runs for t in r['failure_types']})}")
