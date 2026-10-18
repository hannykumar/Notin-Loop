from __future__ import annotations

from typing import Any

from integrations.mock_mcp import MockMCP
from runtime.resilience import run_with_retry
from runtime.settings import get_settings


class LocalToolClient:
    def __init__(self, incident_id: str, allow_write: bool = False):
        self.mcp = MockMCP(incident_id, allow_write=allow_write)

    def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        return self.mcp.call_tool(tool_name, **kwargs)

    def execute_remediation(self, action: dict[str, Any]) -> dict[str, Any]:
        return self.mcp.execute_remediation(action=action)


class MCPToolClient:
    def __init__(self, incident_id: str):
        self.incident_id = incident_id

    def _client(self):
        try:
            from integrations.mcp_client import MCPLiveClient, MCPMockClient
        except Exception as exc:
            raise RuntimeError("MCP transport is unavailable. Install the MCP dependency set first.") from exc

        backend = get_settings().mcp_backend
        if backend == "live":
            return MCPLiveClient()
        return MCPMockClient()

    def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        client = self._client()
        return client.call_tool(tool_name, incident_id=self.incident_id, **kwargs)

    def execute_remediation(self, action: dict[str, Any]) -> dict[str, Any]:
        client = self._client()
        return client.call_tool("execute_remediation", incident_id=self.incident_id, action=action)


class ResilientToolClient:
    def __init__(self, inner: Any):
        self.inner = inner

    def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        settings = get_settings()
        return run_with_retry(
            lambda: self.inner.call_tool(tool_name, **kwargs),
            retries=settings.tool_max_retries,
            timeout_seconds=settings.tool_timeout_seconds,
            operation_name=f"tool:{tool_name}",
        )

    def execute_remediation(self, action: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()
        return run_with_retry(
            lambda: self.inner.execute_remediation(action=action),
            retries=0,
            timeout_seconds=settings.tool_timeout_seconds,
            operation_name="execute_remediation",
        )


def get_tool_client(tool_mode: str, incident_id: str, allow_write: bool = False):
    if tool_mode == "mcp":
        return ResilientToolClient(MCPToolClient(incident_id))
    return ResilientToolClient(LocalToolClient(incident_id, allow_write=allow_write))
