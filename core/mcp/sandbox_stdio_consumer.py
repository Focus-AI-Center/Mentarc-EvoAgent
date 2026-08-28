"""Consumer for stdio MCP servers running inside MinIO sandbox containers."""

from __future__ import annotations

import asyncio

from core.mcp.registry import MCPServerConfig
from core.mcp.sandbox_session import get_sandbox_stdio_session
from utils.logger import logger

_CALL_TIMEOUT_SLACK = 5.0


class SandboxStdioMCPConsumer:
    def __init__(self, *, user_id: str, session_id: str, server: MCPServerConfig):
        self.user_id = user_id
        self.session_id = session_id
        self.server = server

    async def list_tools(self) -> list[dict]:
        try:
            session = await get_sandbox_stdio_session(
                user_id=self.user_id,
                session_id=self.session_id,
                server=self.server,
            )
            return await session.list_tools()
        except Exception as exc:
            logger.warning(f"[mcp-sandbox] list_tools failed server={self.server.name}: {exc}")
            return []

    async def call_tool(self, tool: str, args: dict | None, timeout: float = 60.0) -> dict:
        try:
            session = await get_sandbox_stdio_session(
                user_id=self.user_id,
                session_id=self.session_id,
                server=self.server,
            )
            return await asyncio.wait_for(
                session.call_tool(tool, args or {}, timeout), timeout=timeout + _CALL_TIMEOUT_SLACK
            )
        except TimeoutError:
            return {"error": f"MCP tool {self.server.name}/{tool} timed out", "error_type": "timeout"}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "error_type": "sandbox_error"}


__all__ = ["SandboxStdioMCPConsumer"]
