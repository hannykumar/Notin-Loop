from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from executor.schemas import ExecutionRequest, RollbackRequest
from executor.service import ExecutionService
from runtime.db import init_db
from runtime.settings import get_settings

app = FastAPI(title="sre-executor-service")
init_db()


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/execute")
def execute(request: ExecutionRequest, x_executor_token: str | None = Header(default=None)) -> dict:
    shared_token = get_settings().executor_shared_token
    if shared_token and (x_executor_token or "").strip() != shared_token:
        raise HTTPException(status_code=403, detail="Missing or invalid executor token")
    return ExecutionService().execute(request)


@app.post("/rollback")
def rollback(request: RollbackRequest, x_executor_token: str | None = Header(default=None)) -> dict:
    shared_token = get_settings().executor_shared_token
    if shared_token and (x_executor_token or "").strip() != shared_token:
        raise HTTPException(status_code=403, detail="Missing or invalid executor token")
    execution_request = ExecutionRequest(
        execution_id=request.execution_id,
        run_id=request.run_id,
        incident_id=request.incident_id,
        incident=request.incident,
        action=request.action,
        execution_mode=request.execution_mode,
        tool_mode=request.tool_mode,
    )
    return ExecutionService().rollback(execution_request)
