"""
Small MCP stdio client for the mock server.

The LangGraph agent uses this when tool_mode=mcp.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any


class StdioMCPClient:
    def __init__(self, server_command: str | None = None, server_args: list[str] | None = None):
        self.server_command = server_command or sys.executable
        self.server_args = server_args or []

    def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        return asyncio.run(self._call_tool(tool_name, kwargs))

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self.server_command,
            args=self.server_args,
            env=os.environ.copy(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return self._decode_result(result)

    def _decode_result(self, result: Any) -> Any:
        content = getattr(result, "content", None)
        if bool(getattr(result, "isError", False)):
            detail = "\n".join(str(item.text) for item in (content or []) if hasattr(item, "text"))
            raise RuntimeError(detail or "MCP tool returned an error result")

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]

        if not content:
            return None

        if len(content) == 1 and hasattr(content[0], "text"):
            text = content[0].text
            try:
                return json.loads(text)
            except Exception:
                return text

        blocks = []
        for item in content:
            if hasattr(item, "text"):
                blocks.append(item.text)
        joined = "\n".join(blocks)
        try:
            return json.loads(joined)
        except Exception:
            return joined


class MCPMockClient(StdioMCPClient):
    def __init__(self, server_command: str | None = None, server_args: list[str] | None = None):
        super().__init__(server_command=server_command, server_args=server_args or ["-m", "integrations.mock_mcp_server"])


class MCPLiveClient(StdioMCPClient):
    def __init__(self, server_command: str | None = None, server_args: list[str] | None = None):
        super().__init__(server_command=server_command, server_args=server_args or ["-m", "integrations.live_mcp_server"])
