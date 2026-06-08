"""MCP tool integration for Mentarc-EvoAgent."""

from __future__ import annotations


def __getattr__(name: str):
    if name == "MCPToolManager":
        from core.mcp.manager import MCPToolManager

        return MCPToolManager
    if name == "close_all_sandbox_stdio_sessions":
        from core.mcp.sandbox_session import close_all_sandbox_stdio_sessions

        return close_all_sandbox_stdio_sessions
    raise AttributeError(name)


__all__ = ["MCPToolManager", "close_all_sandbox_stdio_sessions"]
