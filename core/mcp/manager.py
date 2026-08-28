"""MCP tool manager: registry -> SDK FunctionTool list."""

from __future__ import annotations

import asyncio

from agents import FunctionTool

from core.mcp.adapter import mcp_tools_to_function_tools
from core.mcp.http_consumer import HTTPMCPConsumer
from core.mcp.registry import MCPServerConfig, load_mcp_registry
from core.mcp.sandbox_stdio_consumer import SandboxStdioMCPConsumer
from utils.logger import logger


class MCPToolManager:
    def __init__(self, *, user_id: str, session_id: str, workspace):
        self.user_id = user_id
        self.session_id = session_id
        self.workspace = workspace

    def _consumer_for(self, server: MCPServerConfig):
        if server.transport == "stdio":
            return SandboxStdioMCPConsumer(
                user_id=self.user_id,
                session_id=self.session_id,
                server=server,
            )
        return HTTPMCPConsumer(server)

    async def _tools_for_server(self, server: MCPServerConfig) -> list[FunctionTool]:
        consumer = self._consumer_for(server)
        catalog = await consumer.list_tools()
        if not catalog:
            return []
        return mcp_tools_to_function_tools(server, consumer, catalog)

    async def create_tools(self, existing_names: set[str] | None = None) -> list[FunctionTool]:
        """Load enabled MCP servers and return SDK FunctionTool instances."""
        registry = await load_mcp_registry(self.workspace)
        servers = registry.enabled_servers
        if not servers:
            return []
        chunks = await asyncio.gather(
            *(self._tools_for_server(server) for server in servers),
            return_exceptions=True,
        )
        seen = set(existing_names or set())
        tools: list[FunctionTool] = []
        for chunk in chunks:
            if isinstance(chunk, Exception):
                logger.warning(f"[mcp-manager] failed to collect server tools: {chunk}")
                continue
            for tool in chunk:
                name = getattr(tool, "name", "")
                if not name:
                    continue
                if name in seen:
                    logger.warning(f"[mcp-manager] duplicate tool skipped: {name}")
                    continue
                seen.add(name)
                tools.append(tool)
        if tools:
            logger.info(
                f"[mcp-manager] loaded {len(tools)} MCP tools for user={self.user_id} session={self.session_id}"
            )
        return tools


__all__ = ["MCPToolManager"]
