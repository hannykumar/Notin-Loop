"""One isolated preview benchmark at a time, with results separate from saved evidence."""

from pathlib import Path
import os
import subprocess
import sys
import threading
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from runtime.auth import require_role
from runtime.settings import get_settings

router = APIRouter(prefix="/evaluations")
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "eval_history" / "workbench_latest.json"
_lock = threading.Lock()
_process: subprocess.Popen | None = None


class EvaluationRequest(BaseModel):
    provider: Literal["deterministic", "ollama", "openai_compatible"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("/latest", dependencies=[Depends(require_role("viewer"))])
def latest():
    path = OUTPUT if OUTPUT.is_file() else ROOT / "evaluation" / "benchmark_matrix_results.json"
    if not path.is_file():
        return {"profiles": {}}
    return json.loads(path.read_text())


@router.get("/status", dependencies=[Depends(require_role("viewer"))])
def status():
    with _lock:
        if _process is None:
            return {"status": "idle"}
        code = _process.poll()
        return {"status": "running" if code is None else "complete" if code == 0 else "failed", "exit_code": code}


@router.post("", dependencies=[Depends(require_role("operator"))])
def start(request: EvaluationRequest = EvaluationRequest()):
    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            raise HTTPException(409, "An evaluation is already running")
        settings = get_settings()
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "SRE_MCP_BACKEND": "mock", "DATABASE_URL": f"sqlite:///{OUTPUT.parent / 'evaluation.db'}", "SRE_MOCK_STATE_DIR": str(OUTPUT.parent / "mock_state"), "SRE_MAX_ASYNC_WORKERS": "1"}
        # ponytail: one API process owns this local job; use a durable queue for multiple API replicas.
        with (OUTPUT.parent / "workbench.log").open("w") as log:
            _process = subprocess.Popen(
                [sys.executable, "-m", "evaluation.benchmark_matrix", "--model-provider", request.provider or settings.planner_provider,
                 "--model", request.model or settings.planner_model, "--execution-mode", "preview", "--output", str(OUTPUT)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        return {"status": "running"}
