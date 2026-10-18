from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    execution_id: str
    run_id: str = Field(default="")
    incident_id: str
    incident: Dict[str, Any]
    action: Dict[str, Any]
    execution_mode: str
    tool_mode: str
    diagnosis: str = Field(default="Unknown")
    evidence_before: Dict[str, Any] = Field(default_factory=dict)
    rollback_commands: List[str] = Field(default_factory=list)


class ExecutionResponse(BaseModel):
    execution_id: str
    status: str
    execution_results: List[Dict[str, Any]]
    evidence_after: Dict[str, Any] = Field(default_factory=dict)
    improved: bool = False
    improvement_summary: str = ""
    verification: Dict[str, Any] = Field(default_factory=dict)
    rollback_record: Dict[str, Any] = Field(default_factory=dict)
    executor_transport: str = "service"


class RollbackRequest(BaseModel):
    execution_id: str
    run_id: str = Field(default="")
    incident_id: str
    incident: Dict[str, Any]
    action: Dict[str, Any]
    execution_mode: str
    tool_mode: str
