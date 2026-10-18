"""Hermetic test session: a fresh target_service repo at release-0 and an empty runs dir.

Without this, tests read the real factory_flow/target_service and runs/, so they fail after a live demo
(prod is at release-1) and could disturb a running one. Set before any test module is imported, because
some modules resolve workspace.target_dir() at import time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REAL_TARGET = Path(__file__).resolve().parents[1] / "target_service"
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="factory_tests_"))


def _fresh_target() -> Path:
    target = _SESSION_DIR / "target_service"
    shutil.copytree(REAL_TARGET, target, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    subprocess.run([str(target / "scripts" / "init_repo.sh")], check=True, capture_output=True)
    return target


os.environ["FACTORY_TARGET_SERVICE"] = str(_fresh_target())
os.environ["FACTORY_RUNS_DIR"] = str(_SESSION_DIR / "runs")
os.environ.setdefault("TRAFFIC_CONTROL", str(_SESSION_DIR / "runs" / "traffic_control.json"))


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)
