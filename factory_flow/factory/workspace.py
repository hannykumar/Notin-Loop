"""Worktrees, apply diff, pytest, start/stop instances (PRD B2).

Module-level functions; the module itself satisfies contracts.Workspace
(`fresh_worktree(ref)`, `run_pytest(path, selector)`), plus apply_diff,
start_instance / stop_instance and cleanup.

Patterned on SRE-agent/scripts/start_stack.sh @ b4f7e0c: services run as
`uvicorn <module>:app --host 127.0.0.1 --port N` with PYTHONPATH set to the
code directory, and are killed by a cleanup step (there: `trap cleanup EXIT`;
here: a process registry + cleanup() callers run in `finally`).
Patterned on SRE-agent/executor/gitops.py @ b4f7e0c: change operations return
a plain result dict instead of raising, and paths are resolved and checked
before use.
New code: git worktree / git apply / junit parsing (no equivalent in SRE-agent).
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from factory.audit import runs_dir
from factory.contracts import PytestFailure, PytestResult

FACTORY_FLOW_DIR = Path(__file__).resolve().parents[1]


def target_dir() -> Path:
    """target_service git repo (FACTORY_TARGET_SERVICE overrides), read at call time."""
    return Path(os.environ.get("FACTORY_TARGET_SERVICE", FACTORY_FLOW_DIR / "target_service"))


def server_log() -> Path:
    return runs_dir() / "server.log"


def __getattr__(name: str) -> Any:   # old constant names, resolved per access
    if name in ("TARGET_DIR", "RUNS_DIR", "SERVER_LOG"):
        return {"TARGET_DIR": target_dir, "RUNS_DIR": runs_dir, "SERVER_LOG": server_log}[name]()
    raise AttributeError(name)

PYTEST_TIMEOUT_S = 120
GIT_TIMEOUT_S = 60
START_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 5.0

# worktree path -> temp parent dir (removed on cleanup)
_WORKTREES: dict[Path, Path] = {}
# instance name -> {"proc", "port", "path", "out"}
_INSTANCES: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------- git helpers

def _git(args: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), input=stdin, capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
    )


def fresh_worktree(ref: str) -> Path:
    """New detached git worktree of target_service at ref, in a temp dir."""
    if not (target_dir() / ".git").exists():
        raise RuntimeError(f"{target_dir()} is not a git repo; run target_service/scripts/init_repo.sh")
    parent = Path(tempfile.mkdtemp(prefix="sre-wt-"))
    path = parent / "target_service"
    proc = _git(["worktree", "add", "--detach", str(path), ref], target_dir())
    if proc.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        raise RuntimeError(f"git worktree add {ref!r} failed: {proc.stderr.strip()}")
    _WORKTREES[path] = parent
    return path


def remove_worktree(path: Path) -> None:
    path = Path(path)
    parent = _WORKTREES.pop(path, path.parent)
    _git(["worktree", "remove", "--force", str(path)], target_dir())
    shutil.rmtree(parent if parent.name.startswith("sre-wt-") else path, ignore_errors=True)
    _git(["worktree", "prune"], target_dir())


def list_worktrees() -> list[Path]:
    return list(_WORKTREES)


def apply_diff(path: Path, diff: str) -> dict:
    """`git apply --check` then `git apply` inside path. Never raises on a bad diff."""
    path = Path(path).resolve()
    if not diff or not diff.strip():
        return {"applied": False, "error": "empty diff"}
    if not diff.endswith("\n"):
        diff += "\n"
    try:
        check = _git(["apply", "--check", "-"], path, stdin=diff)
        if check.returncode != 0:
            return {"applied": False, "error": (check.stderr or check.stdout).strip() or "git apply --check failed"}
        done = _git(["apply", "-"], path, stdin=diff)
        if done.returncode != 0:
            return {"applied": False, "error": (done.stderr or done.stdout).strip() or "git apply failed"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"applied": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"applied": True, "error": ""}


# ---------------------------------------------------------------- pytest

_TYPE_AT_START = re.compile(r"^\s*([A-Za-z_][\w.]*)(?::|$)")
_E_LINE = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning))\b", re.M)


def _exception_type(message: str, text: str) -> str:
    """Exception type from a junit <failure>/<error> element."""
    m = _TYPE_AT_START.match(message or "")
    if m and m.group(1).rsplit(".", 1)[-1][0].isupper():
        return m.group(1).rsplit(".", 1)[-1]
    found = _E_LINE.findall(text or "")
    if found:
        return found[-1].rsplit(".", 1)[-1]
    if (message or "").lstrip().startswith("assert") or "AssertionError" in (text or ""):
        return "AssertionError"
    return "Unknown"


def _nodeid(file_: str, classname: str, name: str) -> str:
    """Rebuild a pytest nodeid (file.py[::Class]::test) from xunit1 testcase attributes."""
    if not classname:                      # collection error: name is the dotted module
        return file_ or name
    module = file_[:-3].replace("/", ".") if file_.endswith(".py") else ""
    if not file_:
        return f"{classname}::{name}"
    rest = classname[len(module) + 1:] if module and classname.startswith(module + ".") else ""
    return "::".join([file_, *(rest.split(".") if rest else []), name])


def parse_junit(junit_path: Path) -> dict:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures: list[PytestFailure] = []
    root = ET.parse(junit_path).getroot()
    for case in root.iter("testcase"):
        nodeid = _nodeid(case.get("file") or "", case.get("classname") or "", case.get("name") or "")
        outcome = "passed"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome = child.tag
                if child.tag != "skipped":
                    message = child.get("message", "")
                    failures.append(PytestFailure(
                        nodeid=nodeid, type=_exception_type(message, child.text or ""), message=message[:500],
                    ))
                break
        counts[{"failure": "failed", "error": "errors"}.get(outcome, outcome)] += 1
    return {**counts, "failures": failures}


def run_pytest(path: Path, selector: str) -> PytestResult:
    """Run pytest with --junitxml inside path (own cwd and PYTHONPATH)."""
    path = Path(path).resolve()
    fd, junit = tempfile.mkstemp(prefix="sre-junit-", suffix=".xml")
    os.close(fd)
    env = {**os.environ, "PYTHONPATH": str(path), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("SRE_INSTANCE", None)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1", *selector.split(), f"--junitxml={junit}"]
    try:
        proc = subprocess.run(cmd, cwd=str(path), env=env, capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        return PytestResult(passed=0, failed=0, errors=1, skipped=0, junit_path=junit, exit_code=-1, failures=[
            PytestFailure(nodeid=selector, type="Timeout", message=f"pytest exceeded {PYTEST_TIMEOUT_S}s")])
    try:
        parsed = parse_junit(Path(junit))
    except (ET.ParseError, OSError) as exc:
        tail = (proc.stdout + proc.stderr)[-500:]
        parsed = {"passed": 0, "failed": 0, "errors": 1, "skipped": 0, "failures": [
            PytestFailure(nodeid=selector, type="NoJunit", message=f"{type(exc).__name__}: {tail}")]}
    # exit 2-5 (interrupted / internal / usage error / no tests collected) is never a valid red or green
    if exit_code in (2, 3, 4, 5) and parsed["errors"] == 0 and parsed["failed"] == 0:
        parsed["errors"] = 1
        parsed["failures"].append(PytestFailure(nodeid=selector, type="PytestError",
                                                message=(proc.stdout + proc.stderr)[-500:]))
    return PytestResult(passed=parsed["passed"], failed=parsed["failed"], errors=parsed["errors"],
                        skipped=parsed["skipped"], failures=parsed["failures"], junit_path=junit, exit_code=exit_code)


# ---------------------------------------------------------------- instances

def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _wait_up(port: int, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def start_instance(path: Path, port: int, name: str, log_path: Path | None = None) -> dict:
    """Run uvicorn app.main:app on 127.0.0.1:port from path; wait until GET / answers.

    name is the SRE_INSTANCE value ("stable" | "canary") and the registry key.
    SRE_LOG_PATH is server_log() (<runs dir>/server.log) unless log_path is given.
    Returns {"name", "port", "pid", "path"}. Raises RuntimeError if busy or not up in time.
    """
    path = Path(path).resolve()
    if name in _INSTANCES and _INSTANCES[name]["proc"].poll() is None:
        raise RuntimeError(f"instance {name!r} already running on port {_INSTANCES[name]['port']}")
    if port_busy(port):
        raise RuntimeError(f"port {port} is busy")
    env = {**os.environ, "PYTHONPATH": str(path), "PYTHONDONTWRITEBYTECODE": "1",
           "SRE_INSTANCE": name, "SRE_LOG_PATH": str(log_path or server_log())}
    out = open(runs_dir() / f"instance-{name}.out", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(path), env=env, stdout=out, stderr=subprocess.STDOUT,
    )
    _INSTANCES[name] = {"proc": proc, "port": port, "path": path, "out": out}
    if not _wait_up(port, proc, START_TIMEOUT_S):
        stop_instance(name)
        raise RuntimeError(f"instance {name!r} did not answer on port {port}; see {out.name}")
    return {"name": name, "port": port, "pid": proc.pid, "path": str(path)}


def stop_instance(name: str) -> bool:
    """Terminate (then kill) the named instance. Returns False if it was not registered."""
    entry = _INSTANCES.pop(name, None)
    if entry is None:
        return False
    proc: subprocess.Popen = entry["proc"]
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=STOP_TIMEOUT_S)
    finally:
        entry["out"].close()
    return True


def instances() -> dict[str, dict]:
    return {n: {"port": e["port"], "pid": e["proc"].pid, "path": str(e["path"])} for n, e in _INSTANCES.items()}


def cleanup() -> None:
    """Stop ALL instances (stable too) and remove all worktrees this process created: process shutdown only.
    Stages remove just their own worktrees with remove_worktree()."""
    for name in list(_INSTANCES):
        try:
            stop_instance(name)
        except Exception:  # noqa: BLE001 - cleanup must keep going
            pass
    for path in list(_WORKTREES):
        try:
            remove_worktree(path)
        except Exception:  # noqa: BLE001
            pass
    if (target_dir() / ".git").exists():
        _git(["worktree", "prune"], target_dir())
