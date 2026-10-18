"""Shared Track A test support: a git repo of the mini_service fixture and a fake workspace.

StubWorkspace is a test double for factory/workspace.py with the frozen Workspace / PytestResult shapes
(contracts.py); it tracks its worktrees so tests can assert none are left behind.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MINI_SERVICE = FIXTURES / "mini_service"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_mini_repo(dest: Path) -> Path:
    """Copy mini_service into dest as its own git repo with branches main, prod and tag release-0."""
    repo = dest / "target_service"
    shutil.copytree(MINI_SERVICE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=agent@factory", "-c", "user.name=factory", "add", "-A")
    git(repo, "-c", "user.email=agent@factory", "-c", "user.name=factory", "commit", "-q", "-m", "release-0")
    git(repo, "branch", "prod")
    git(repo, "tag", "release-0")
    return repo


class StubWorkspace:
    def __init__(self, repo: Path):
        self.repo = repo
        self._trees: list[Path] = []

    def fresh_worktree(self, ref: str = "prod") -> Path:
        path = Path(tempfile.mkdtemp(prefix="wt_")) / "tree"
        git(self.repo, "worktree", "add", "-q", "--detach", str(path), ref)
        self._trees.append(path)
        return path

    def apply_diff(self, path: Path, diff: str) -> None:
        for args in (["apply", "--check", "-"], ["apply", "-"]):
            subprocess.run(["git", *args], cwd=path, input=diff, text=True, capture_output=True, check=True)

    def run_pytest(self, path: Path, selector: str = "") -> dict:
        junit = Path(tempfile.mkdtemp(prefix="junit_")) / "junit.xml"
        env = {**os.environ, "PYTHONPATH": str(path)}
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"]
        if selector:
            cmd.append(selector)
        proc = subprocess.run(cmd, cwd=path, env=env, capture_output=True, text=True, timeout=120)
        result = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "failures": [],
                  "junit_path": str(junit), "exit_code": proc.returncode}
        if not junit.exists():
            result["errors"] = 1
            result["failures"].append({"nodeid": selector, "type": "CollectionError", "message": proc.stdout[-500:]})
            return result
        for case in ET.parse(junit).getroot().iter("testcase"):
            nodeid = f"{case.get('classname')}::{case.get('name')}"
            child = next(iter(case), None)
            if child is None or child.tag in ("system-out", "system-err", "properties"):
                result["passed"] += 1
            elif child.tag == "skipped":
                result["skipped"] += 1
            else:
                key = "failed" if child.tag == "failure" else "errors"
                result[key] += 1
                message = child.get("message") or ""
                exc_type = message.split(":", 1)[0].strip() or child.get("type", "")
                result["failures"].append({"nodeid": nodeid, "type": exc_type, "message": message[:500]})
        return result

    def remove_worktree(self, path: Path) -> None:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo, capture_output=True)
        self._trees.remove(path)

    def cleanup(self) -> None:
        for path in self._trees:
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo, capture_output=True)
        self._trees.clear()
