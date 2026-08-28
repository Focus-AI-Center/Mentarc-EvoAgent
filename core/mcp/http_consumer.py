"""HTTP/SSE MCP consumer."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from core.mcp.registry import MCPServerConfig
from utils.logger import logger

_LIST_TIMEOUT = 10.0
_CALL_TIMEOUT_SLACK = 5.0


def _content_to_jsonable(content: list) -> list[dict]:
    out: list[dict] = []
    for block in content or []:
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(mode="json", exclude_none=True))
        elif isinstance(block, dict):
            out.append(block)
        else:
            out.append({"type": "text", "text": str(block)})
    return out


def _tool_to_jsonable(tool: Any) -> dict:
    if hasattr(tool, "model_dump"):
        d = tool.model_dump(mode="json", exclude_none=True)
    elif isinstance(tool, dict):
        d = tool
    else:
        d = dict(tool)
    return {
        "name": d.get("name", ""),
        "description": d.get("description") or "",
        "input_schema": d.get("inputSchema") or d.get("input_schema") or {"type": "object"},
    }


class HTTPMCPConsumer:
    def __init__(self, server: MCPServerConfig):
        self.server = server

    async def _open_session(self):
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError("mcp Python SDK is not installed") from exc

        stack = AsyncExitStack()
        if self.server.transport == "sse":
            read, write = await stack.enter_async_context(
                sse_client(self.server.url, headers=self.server.headers or None)
            )
        else:
            streams = await stack.enter_async_context(
                streamablehttp_client(self.server.url, headers=self.server.headers or None)
            )
            read, write = streams[0], streams[1]
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return stack, session

    async def list_tools(self) -> list[dict]:
        try:
            async with asyncio.timeout(_LIST_TIMEOUT):
                stack, session = await self._open_session()
                async with stack:
                    result = await session.list_tools()
                    return [_tool_to_jsonable(t) for t in getattr(result, "tools", [])]
        except Exception as exc:
            logger.warning(f"[mcp-http] list_tools failed server={self.server.name}: {exc}")
            return []

    async def call_tool(self, tool: str, args: dict | None, timeout: float = 60.0) -> dict:
        try:
            async with asyncio.timeout(timeout + _CALL_TIMEOUT_SLACK):
                stack, session = await self._open_session()
                async with stack:
                    result = await session.call_tool(tool, args or {})
                    return {
                        "content": _content_to_jsonable(getattr(result, "content", []) or []),
                        "is_error": bool(
                            getattr(result, "isError", False) or getattr(result, "is_error", False)
                        ),
                    }
        except TimeoutError:
            return {"error": f"MCP tool {self.server.name}/{tool} timed out", "error_type": "timeout"}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "error_type": "transport_error"}


__all__ = ["HTTPMCPConsumer"]
