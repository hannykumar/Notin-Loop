"""Stage 7: static rules on the diff (PRD B3).

Runs before any patched code executes. A pure function over the diff text (plus an optional worktree used
only to parse the patched files with ast; nothing is imported or executed).

# Patterned on SRE-agent/integrations/query_safety.py @ b4f7e0c (validate-and-explain: every rule is a small
# named validator that produces a verdict plus a human-readable reason). Here each rule returns
# (ok, reason) instead of raising, so all rules are evaluated and reported together in one Verdict.
New code otherwise: SRE-agent has no unified-diff parser or code-diff policy.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from factory.contracts import RunState, Verdict
from factory.policy import (
    APP_DIR,
    BANNED_IMPORTS,
    DEPENDENCY_FILES,
    MAX_FILES,
    MAX_LINES,
    PROTECTED_PATHS,
    SECRET_PATTERNS,
)
from factory.sre_base import canonical_hash

STAGE = "inspect_diff"

# Not in policy.py (frozen after K2): stage-local constants for rules policy.py does not cover.
NEAR_TRACEBACK_LINES = 10                       # a hunk must be within this many lines of a traceback line
TRIVIAL_LITERALS = {0, 1, -1, "", True, False, None, 200, 404, 500}

CHECK_NAMES = [
    "valid_unified_diff",
    "file_count_ok",
    "lines_changed_ok",
    "hunks_near_traceback",
    "no_protected_paths",
    "no_banned_imports",
    "no_secrets",
    "no_dependency_files",
    "no_binary",
    "ast_parses",
    "no_frozen_test_literals",
]

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_IMPORT_LINE_RE = re.compile(r"^\s*(import\s+\w|from\s+[\w.]+\s+import\s)")
_DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    changed_old_lines: list[int] = field(default_factory=list)  # old-file line numbers touched by the hunk


@dataclass
class FileDiff:
    old_path: Optional[str]
    new_path: Optional[str]
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


class DiffParseError(ValueError):
    pass


def _strip_prefix(raw: str) -> Optional[str]:
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw[:2] in ("a/", "b/"):
        raw = raw[2:]
    return raw


def parse_unified_diff(diff: str) -> list[FileDiff]:
    """Parse a git-style unified diff; raise DiffParseError if it is not one."""
    if not diff or not diff.strip():
        raise DiffParseError("diff is empty")
    files: list[FileDiff] = []
    current: Optional[FileDiff] = None
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = FileDiff(old_path=None, new_path=None)
            parts = line.split(" ")
            if len(parts) >= 4:
                current.old_path, current.new_path = _strip_prefix(parts[2]), _strip_prefix(parts[3])
            files.append(current)
            i += 1
            continue
        if line.startswith("GIT binary patch") or (line.startswith("Binary files ") and line.endswith(" differ")):
            if current is None:
                current = FileDiff(old_path=None, new_path=None)
                files.append(current)
            current.binary = True
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:])
            if current is None or current.hunks:
                current = FileDiff(old_path=old, new_path=new)
                files.append(current)
            else:
                current.old_path, current.new_path = old, new
            i += 2
            continue
        match = _HUNK_RE.match(line)
        if match:
            if current is None or (current.old_path is None and current.new_path is None):
                raise DiffParseError(f"hunk without ---/+++ file header: {line!r}")
            hunk = Hunk(
                old_start=int(match.group(1)),
                old_len=int(match.group(2) if match.group(2) is not None else 1),
                new_start=int(match.group(3)),
                new_len=int(match.group(4) if match.group(4) is not None else 1),
            )
            old_seen = new_seen = 0
            i += 1
            while i < len(lines) and (old_seen < hunk.old_len or new_seen < hunk.new_len):
                body = lines[i]
                old_line_no = hunk.old_start + old_seen
                if body.startswith("\\"):        # "\ No newline at end of file"
                    i += 1
                    continue
                if body.startswith("+"):
                    hunk.added.append(body[1:])
                    hunk.changed_old_lines.append(old_line_no)
                    new_seen += 1
                elif body.startswith("-"):
                    hunk.removed.append(body[1:])
                    hunk.changed_old_lines.append(old_line_no)
                    old_seen += 1
                elif body.startswith(" ") or body == "":
                    hunk.context.append(body[1:])
                    old_seen += 1
                    new_seen += 1
                else:
                    raise DiffParseError(f"unexpected line inside hunk: {body!r}")
                i += 1
            if old_seen != hunk.old_len or new_seen != hunk.new_len:
                raise DiffParseError(f"hunk line counts do not match header @@ -{hunk.old_start},{hunk.old_len} "
                                     f"+{hunk.new_start},{hunk.new_len} @@")
            current.hunks.append(hunk)
            continue
        i += 1
    if not files:
        raise DiffParseError("no file headers (diff --git / --- / +++) found")
    for f in files:
        if not f.binary and not f.hunks:
            raise DiffParseError(f"file {f.path or '?'} has no @@ hunks")
    return files


# --- Rules: each returns (ok, reason). Reasons name the rule. ---------------------------------

def _traceback_frames(traceback: str) -> list[tuple[str, int, str]]:
    return [(path, int(line), func) for path, line, func in _FRAME_RE.findall(traceback or "")]


def _frame_matches(frame_path: str, diff_path: str) -> bool:
    frame_path = frame_path.replace("\\", "/")
    return frame_path == diff_path or frame_path.endswith("/" + diff_path)


def _hunk_is_import_only(hunk: Hunk) -> bool:
    changed = [l for l in hunk.added + hunk.removed if l.strip()]
    return bool(changed) and all(_IMPORT_LINE_RE.match(l) for l in changed)


def _hunk_redefines_traceback_name(hunk: Hunk, traceback: str) -> bool:
    """The hunk edits the definition of a function/class named in the traceback (e.g. get_order in
    `return get_order(order_id).to_dict()`), which is the cause even though it is not a frame."""
    for line in hunk.removed:
        match = _DEF_RE.match(line)
        if match and re.search(rf"\b{re.escape(match.group(1))}\b", traceback):
            return True
    return False


def check_hunks_near_traceback(files: list[FileDiff], traceback: str) -> tuple[Optional[bool], str]:
    """None => missing evidence (unsure)."""
    frames = _traceback_frames(traceback)
    if not frames:
        return None, "hunks_near_traceback: no traceback (or no frames) supplied, cannot check fix locality"
    far: list[str] = []
    for f in files:
        lines = [line for path, line, _ in frames if _frame_matches(path, f.path)]
        for hunk in f.hunks:
            if not lines:
                far.append(f"{f.path} (file not in traceback)")
                continue
            lo, hi = min(hunk.changed_old_lines), max(hunk.changed_old_lines)
            near = any(lo - NEAR_TRACEBACK_LINES <= tb <= hi + NEAR_TRACEBACK_LINES for tb in lines)
            if not (near or _hunk_is_import_only(hunk) or _hunk_redefines_traceback_name(hunk, traceback)):
                far.append(f"{f.path}:{lo}-{hi} (traceback lines {sorted(set(lines))})")
    if far:
        return False, f"hunks_near_traceback: hunks more than {NEAR_TRACEBACK_LINES} lines from the traceback: {'; '.join(far)}"
    return True, ""


def _added_lines(files: list[FileDiff]) -> list[tuple[str, str]]:
    return [(f.path, line) for f in files for h in f.hunks for line in h.added]


def frozen_test_literals(frozen_test: str) -> Optional[set]:
    """Non-trivial str/number constants in the frozen test (docstrings excluded). None if unparsable."""
    try:
        tree = ast.parse(frozen_test)
    except SyntaxError:
        return None
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docstrings:
            value = node.value
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (str, int, float)) and value not in TRIVIAL_LITERALS:
                literals.add(value)
    return literals


def _literal_pattern(value) -> re.Pattern:
    if isinstance(value, str):
        return re.compile(r"""(['"])""" + re.escape(value) + r"\1")
    return re.compile(r"(?<![\w.])" + re.escape(repr(value)) + r"(?![\w.])")


def check_frozen_test_literals(files: list[FileDiff], frozen_test: str,
                               worktree: Optional[Path]) -> tuple[Optional[bool], str]:
    if not frozen_test or not frozen_test.strip():
        return None, "no_frozen_test_literals: no frozen test supplied, cannot check for hardcoded expected values"
    literals = frozen_test_literals(frozen_test)
    if literals is None:
        return None, "no_frozen_test_literals: frozen test does not parse, cannot extract its literals"
    hits: list[str] = []
    for f in files:
        if not f.path.endswith(".py"):
            continue
        # Literals the file already used (context/removed lines or the pre-image) are not new hardcoding.
        existing = "\n".join(l for h in f.hunks for l in h.context + h.removed)
        if worktree is not None and f.old_path and (Path(worktree) / f.old_path).is_file():
            existing += "\n" + (Path(worktree) / f.old_path).read_text(errors="replace")
        for hunk in f.hunks:
            for line in hunk.added:
                for value in literals:
                    pattern = _literal_pattern(value)
                    if pattern.search(line) and not pattern.search(existing):
                        hits.append(f"{f.path}: {value!r} in `{line.strip()}`")
    if hits:
        return False, f"no_frozen_test_literals: added source hardcodes values from the frozen test: {'; '.join(hits)}"
    return True, ""


def _git_apply(diff: str, cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "apply", "--whitespace=nowarn", *extra, "-"], input=diff, cwd=cwd,
                          capture_output=True, text=True)


def check_ast_parses(files: list[FileDiff], diff: str, worktree: Optional[Path]) -> tuple[Optional[bool], str]:
    """Apply the diff to a temp copy of the touched files and ast.parse each patched .py file.
    If the worktree already has the diff applied, parse the post-image in place."""
    py_files = [f for f in files if f.new_path and f.new_path.endswith(".py")]
    if not py_files:
        return True, ""
    if worktree is None:
        return None, "ast_parses: no worktree supplied, cannot parse the patched files"
    worktree = Path(worktree)
    with tempfile.TemporaryDirectory(prefix="inspect_diff_") as tmp:
        tmp_dir = Path(tmp)
        for f in files:
            if f.old_path and (worktree / f.old_path).is_file():
                (tmp_dir / f.old_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(worktree / f.old_path, tmp_dir / f.old_path)
        subprocess.run(["git", "init", "-q"], cwd=tmp_dir, capture_output=True)  # so git apply uses repo-relative paths
        applied = _git_apply(diff, tmp_dir)
        if applied.returncode == 0:
            root = tmp_dir
        elif _git_apply(diff, worktree, "--reverse", "--check").returncode == 0:
            root = worktree                                  # already applied in the worktree
        else:
            return False, f"ast_parses: diff does not apply to {worktree}: {applied.stderr.strip()[:300]}"
        errors = []
        for f in py_files:
            try:
                ast.parse((root / f.new_path).read_text(), filename=f.new_path)
            except (SyntaxError, OSError) as exc:
                errors.append(f"{f.new_path}: {exc}")
    if errors:
        return False, f"ast_parses: patched files do not parse: {errors}"
    return True, ""


def inspect(diff: str, *, traceback: str = "", frozen_test: str = "", worktree: Path | None = None) -> Verdict:
    """Static inspection of a fixer diff. pass = every rule holds; fail = a rule is violated;
    unsure = no rule violated but evidence for some rule (traceback, frozen test, worktree) is missing."""
    checks: dict[str, bool] = {name: False for name in CHECK_NAMES}
    failed: list[str] = []
    missing: list[str] = []
    evidence = {"diff_sha256": canonical_hash(diff or ""), "files": [], "lines_changed": 0}

    def record(name: str, ok: Optional[bool], reason: str) -> None:
        checks[name] = bool(ok)
        if ok is None:
            missing.append(reason)
        elif not ok:
            failed.append(reason)

    try:
        files = parse_unified_diff(diff)
    except DiffParseError as exc:
        record("valid_unified_diff", False, f"valid_unified_diff: not a valid unified diff ({exc})")
        return Verdict(stage=STAGE, status="fail", reasons=failed, checks=checks, evidence=evidence)
    record("valid_unified_diff", True, "")

    paths = sorted({p for f in files for p in (f.old_path, f.new_path) if p})
    lines_changed = sum(len(h.added) + len(h.removed) for f in files for h in f.hunks)
    evidence.update(files=paths, lines_changed=lines_changed)
    added = _added_lines(files)

    record("file_count_ok", len(files) <= MAX_FILES,
           f"file_count_ok: {len(files)} files changed, limit is {MAX_FILES}")
    record("lines_changed_ok", lines_changed <= MAX_LINES,
           f"lines_changed_ok: {lines_changed} lines added+removed, limit is {MAX_LINES}")
    record("hunks_near_traceback", *check_hunks_near_traceback(files, traceback))

    protected = [p for p in paths if PROTECTED_PATHS.search(p)]
    outside_app = [p for p in paths if not p.startswith(APP_DIR)]
    record("no_protected_paths", not protected and not outside_app,
           f"no_protected_paths: diff touches protected paths {protected}" if protected
           else f"no_protected_paths: diff touches files outside {APP_DIR}: {outside_app}")

    banned = [f"{p}: `{l.strip()}`" for p, l in added if BANNED_IMPORTS.search(l)]
    record("no_banned_imports", not banned, f"no_banned_imports: added lines use banned imports/calls: {'; '.join(banned)}")

    secrets = [p for p, l in added if any(rx.search(l) for rx in SECRET_PATTERNS)]
    record("no_secrets", not secrets, f"no_secrets: added lines match a secret pattern in {sorted(set(secrets))}")

    deps = [p for p in paths if DEPENDENCY_FILES.search(p)]
    record("no_dependency_files", not deps, f"no_dependency_files: diff edits dependency files {deps}")

    binary = [f.path for f in files if f.binary]
    record("no_binary", not binary, f"no_binary: diff contains binary patches for {binary}")

    record("ast_parses", *check_ast_parses(files, diff, worktree))
    record("no_frozen_test_literals", *check_frozen_test_literals(files, frozen_test, worktree))

    status = "fail" if failed else ("unsure" if missing else "pass")
    reasons = failed + [f"unsure: {r}" for r in missing]
    return Verdict(stage=STAGE, status=status, reasons=reasons, checks=checks, evidence=evidence)


def run(state: RunState, workspace=None) -> Verdict:
    """Stage wrapper: diff from the fixer, traceback from the bug report, frozen test from reproduce.
    The AST / pre-image checks read a fresh worktree of `prod` (nothing in it is executed)."""
    if workspace is None:
        from factory import workspace
    traceback = state.bug.traceback if state.bug else ""
    frozen_test = ""
    if state.failing_test and Path(state.failing_test).is_file():
        frozen_test = Path(state.failing_test).read_text()
    worktree = workspace.fresh_worktree("prod")
    try:
        return inspect(state.diff, traceback=traceback, frozen_test=frozen_test, worktree=worktree)
    finally:
        workspace.remove_worktree(worktree)


__all__ = ["CHECK_NAMES", "DiffParseError", "inspect", "parse_unified_diff", "run"]
