"""Black-box fixers: diff only (PRD A10)."""
from __future__ import annotations

import concurrent.futures
import re
import shutil
import subprocess
from pathlib import Path

from factory import llm, policy, sre_base
from factory.contracts import BugReport, RunState, Verdict

STAGE = "fix"
FIX_SYSTEM = (
    "You fix one bug in a Python web service. Reply with ONLY a unified diff, no prose. "
    "Use '--- a/<path>' and '+++ b/<path>' headers with paths relative to the repository root, "
    "and '@@' hunk headers with exact context lines. Change application code only, never tests. "
    "Keep the change minimal so the failing test passes and the existing tests stay green."
)


def is_unified_diff(text: str) -> bool:
    return "--- a/" in text and "+++ b/" in text and "@@" in text


HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


def normalize_diff(text: str) -> str:
    """Tidy LLM diff output: drop text before the first header, unindent header lines, recount hunk sizes.

    Models often emit a stray leading space before '--- a/' or miscount '@@' line ranges; both make git apply fail.
    """
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip().startswith("--- a/")), None)
    if start is None:
        return text
    out: list[str] = []
    i = start
    while i < len(lines):
        stripped = lines[i].lstrip()
        match = HUNK.match(stripped)
        if not match:   # outside a hunk: keep file headers (unindented), drop prose
            if stripped.startswith(("--- ", "+++ ", "diff --git ", "index ")):
                out.append(stripped)
            i += 1
            continue
        body = []
        i += 1
        while i < len(lines) and not HUNK.match(lines[i].lstrip()) and not lines[i].lstrip().startswith(("--- a/", "diff --git ")):
            body.append(lines[i] if lines[i] else " ")   # an empty line inside a hunk is blank context
            i += 1
        while body and body[-1].strip() == "":          # trailing blank lines are padding, not context
            body.pop()
        old = sum(1 for l in body if l[:1] in (" ", "-"))
        new = sum(1 for l in body if l[:1] in (" ", "+"))
        out.append(f"@@ -{match.group(1)},{old} +{match.group(2)},{new} @@{match.group(3)}")
        out.extend(body)
    return "\n".join(out) + "\n"


class ScriptedFixer:
    """Returns a known-good patch file (target_service/patches/)."""
    name = "scripted"

    def __init__(self, patch_path: Path):
        self.patch_path = Path(patch_path)

    def fix(self, workspace: Path, bug: BugReport, failing_test: Path, timeout_s: int) -> str:
        return self.patch_path.read_text()


class FixtureFixer(ScriptedFixer):
    """Returns a rogue/test fixture diff (tests/fixtures/); separate name for the audit."""
    name = "fixture"


class LLMFixer:
    """Asks the LLM for a unified diff from the bug report, the frozen test and the source."""
    name = "llm"
    attempts = 2  # one retry on invalid output

    def fix(self, workspace: Path, bug: BugReport, failing_test: Path, timeout_s: int) -> str:
        sources = "\n\n".join(
            f"### {f}\n{(workspace / f).read_text()}" for f in bug.files if (workspace / f).is_file()
        )
        user = (
            f"Signature: {bug.signature}\nException: {bug.exception_type}\n"
            f"Traceback:\n{bug.traceback}\nFailing request: {bug.sample_request}\n\n"
            f"Failing test ({failing_test.name}, frozen, do not edit):\n{failing_test.read_text()}\n\n"
            f"Source files:\n{sources}"
        )
        for _ in range(self.attempts):
            diff = llm.complete(FIX_SYSTEM, user)
            if is_unified_diff(diff):
                return normalize_diff(diff)  # also restores the trailing newline git apply needs
        raise ValueError(f"LLM output is not a unified diff after {self.attempts} attempts")


def _cleanup(workspace) -> None:
    if hasattr(workspace, "cleanup"):
        workspace.cleanup()


def _call_fixer(fixer, tree: Path, state: RunState, timeout_s: int) -> str:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fixer.fix, tree, state.bug, tree / "tests" / state.failing_test.name, timeout_s).result(
            timeout=timeout_s
        )
    finally:
        # ponytail: a timed-out thread can't be killed; it is abandoned (process isolation if fixers hang for real)
        pool.shutdown(wait=False, cancel_futures=True)


def _diff_applies(workspace, diff: str) -> tuple[bool, str]:
    tree = workspace.fresh_worktree("prod")
    try:
        proc = subprocess.run(["git", "apply", "--check", "-"], cwd=tree, input=diff, text=True, capture_output=True)
        return proc.returncode == 0, proc.stderr.strip()
    finally:
        _cleanup(workspace)


def run_fix(state: RunState, fixer, *, workspace, timeout_s: int = policy.FIXER_TIMEOUT_S) -> Verdict:
    """Stage 6: run the fixer in a throwaway prod worktree; only its diff string is kept."""
    evidence: dict = {"fixer": getattr(fixer, "name", type(fixer).__name__)}
    checks = {"inputs_present": False, "fixer_returned": False, "diff_nonempty": False, "diff_applies": False}

    def verdict(status: str, reason: str) -> Verdict:
        if isinstance(fixer, LLMFixer):
            evidence["llm"] = llm.status()
        return Verdict(stage=STAGE, status=status, reasons=[reason], checks=checks, evidence=evidence)

    if state.bug is None or state.failing_test is None or not Path(state.failing_test).is_file():
        return verdict("unsure", "missing bug report or frozen failing test")
    checks["inputs_present"] = True

    tree = workspace.fresh_worktree("prod")
    try:
        (tree / "tests").mkdir(exist_ok=True)
        shutil.copyfile(state.failing_test, tree / "tests" / Path(state.failing_test).name)
        diff = _call_fixer(fixer, tree, state, timeout_s)
    except concurrent.futures.TimeoutError:
        return verdict("unsure", f"fixer timed out after {timeout_s}s")
    except Exception as exc:
        return verdict("unsure", f"fixer raised {type(exc).__name__}: {exc}")
    finally:
        _cleanup(workspace)
    checks["fixer_returned"] = True

    if not isinstance(diff, str) or not diff.strip():
        return verdict("unsure", "fixer returned no diff")
    checks["diff_nonempty"] = True
    evidence["diff_sha256"] = sre_base.canonical_hash(diff)

    applies, error = _diff_applies(workspace, diff)
    if not applies:
        return verdict("fail", f"diff does not apply to prod: {error[:300]}")
    checks["diff_applies"] = True
    state.diff = diff
    return verdict("pass", "fixer returned a diff that applies cleanly to prod")
