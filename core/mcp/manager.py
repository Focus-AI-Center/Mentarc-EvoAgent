"""MCP tool manager: registry -> SDK FunctionTool list."""

from __future__ import annotations

import asyncio

from agents import FunctionTool

from core.mcp.adapter import mcp_tools_to_function_tools
from core.mcp.http_consumer import HTTPMCPConsumer
from core.mcp.registry import MCPServerConfig, load_mcp_registry
from core.mcp.sandbox_stdio_consumer import SandboxStdioMCPConsumer
from utils.logger import logger


class MCPToolList(list[FunctionTool]):
    """List-compatible discovery result; failed servers must remain retryable."""

    def __init__(self, tools=(), *, failed_servers: tuple[str, ...] = ()):
        super().__init__(tools)
        self.failed_servers = failed_servers

    @property
    def discovery_complete(self) -> bool:
        return not self.failed_servers


class MCPToolManager:
    def __init__(self, *, user_id: str, session_id: str, workspace):
        self.user_id = user_id
        self.session_id = session_id
        self.workspace = workspace
        self._catalogs: dict[str, tuple[MCPServerConfig, list[FunctionTool]]] = {}

    def _consumer_for(self, server: MCPServerConfig):
        if server.transport == "stdio":
            return SandboxStdioMCPConsumer(
                user_id=self.user_id,
                session_id=self.session_id,
                server=server,
            )
        return HTTPMCPConsumer(server)

    async def _tools_for_server(self, server: MCPServerConfig) -> list[FunctionTool]:
        cached = self._catalogs.get(server.name)
        if cached is not None and cached[0] == server:
            return cached[1]
        consumer = self._consumer_for(server)
        catalog = await consumer.list_tools()
        tools = mcp_tools_to_function_tools(server, consumer, catalog)
        self._catalogs[server.name] = (server, tools)
        return tools

    async def create_tools(self, existing_names: set[str] | None = None) -> MCPToolList:
        """Load enabled MCP servers and return SDK FunctionTool instances."""
        registry = await load_mcp_registry(self.workspace)
        servers = registry.enabled_servers
        if not servers:
            return MCPToolList()
        chunks = await asyncio.gather(
            *(self._tools_for_server(server) for server in servers),
            return_exceptions=True,
        )
        seen = set(existing_names or set())
        tools: list[FunctionTool] = []
        failed: list[str] = []
        for server, chunk in zip(servers, chunks, strict=False):
            if isinstance(chunk, asyncio.CancelledError):
                raise chunk
            if isinstance(chunk, Exception):
                logger.warning(f"[mcp-manager] failed to collect server={server.name} tools: {chunk}")
                failed.append(server.name)
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
        return MCPToolList(tools, failed_servers=tuple(failed))


__all__ = ["MCPToolManager"]
