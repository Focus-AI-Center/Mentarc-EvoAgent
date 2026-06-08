"""Adapter: MCP tool descriptors -> OpenAI Agents SDK FunctionTool."""

from __future__ import annotations

import json
from typing import Protocol

from agents import FunctionTool
from agents.run_context import RunContextWrapper

from core.mcp.registry import MCPServerConfig
from utils.logger import logger


class MCPConsumer(Protocol):
    async def call_tool(self, tool: str, args: dict | None, timeout: float = 60.0) -> dict: ...


def _serialize_content(blocks: list) -> str:
    if not blocks:
        return ""
    parts: list[str] = []
    for block in blocks:
        if hasattr(block, "model_dump"):
            block = block.model_dump(mode="json", exclude_none=True)
        btype = block.get("type") if isinstance(block, dict) else None
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "image":
            mime = block.get("mimeType") or block.get("mime_type") or "image/*"
            parts.append(f"[image: {mime}, {len(block.get('data', '') or '')} bytes b64]")
        elif btype == "resource":
            resource = block.get("resource") if isinstance(block, dict) else None
            uri = resource.get("uri") if isinstance(resource, dict) else block.get("uri")
            parts.append(f"[resource: {uri or '?'}]")
        elif btype is not None:
            parts.append(json.dumps(block, ensure_ascii=False))
        else:
            parts.append(str(block))
    return "\n".join(p for p in parts if p).strip()


def _make_invoker(consumer: MCPConsumer, tool: str, timeout: float):
    async def _invoke(_ctx: RunContextWrapper, args_json: str) -> str:
        try:
            args = json.loads(args_json) if isinstance(args_json, str) and args_json else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid args JSON: {exc}", "error_type": "bad_args"}, ensure_ascii=False)
        result = await consumer.call_tool(tool, args, timeout=timeout)
        if not isinstance(result, dict):
            return json.dumps({"error": f"bad MCP response: {type(result).__name__}", "error_type": "bad_response"}, ensure_ascii=False)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        text = _serialize_content(result.get("content") or [])
        if result.get("is_error"):
            return json.dumps({"error": text or "MCP tool reported error", "error_type": "tool_error"}, ensure_ascii=False)
        return text or "(empty result)"
    return _invoke


def _tool_to_dict(tool) -> dict:
    if hasattr(tool, "model_dump"):
        return tool.model_dump(mode="json", exclude_none=True)
    if isinstance(tool, dict):
        return tool
    return dict(tool)


def mcp_tools_to_function_tools(
    server: MCPServerConfig,
    consumer: MCPConsumer,
    catalog: list,
) -> list[FunctionTool]:
    """Wrap MCP catalog entries as OpenAI Agents SDK tools."""
    tools: list[FunctionTool] = []
    prefix = "mcp__sandbox" if server.source_kind == "sandbox" else "mcp__server"
    for raw in catalog or []:
        try:
            entry = _tool_to_dict(raw)
        except Exception:
            continue
        name = str(entry.get("name", "")).strip()
        if not name or not server.tool_filter.allows(name):
            continue
        agent_tool_name = f"{prefix}__{server.name}__{name}"
        description = (entry.get("description") or "").strip()
        origin = "sandbox stdio MCP" if server.source_kind == "sandbox" else "MCP server"
        description = f"{description}\n\n[via {origin}: {server.name}]" if description else f"[via {origin}: {server.name}]"
        schema = entry.get("inputSchema") or entry.get("input_schema") or {"type": "object"}
        if not isinstance(schema, dict):
            schema = {"type": "object"}
        try:
            tools.append(
                FunctionTool(
                    name=agent_tool_name,
                    description=description,
                    params_json_schema=schema,
                    on_invoke_tool=_make_invoker(consumer, name, server.timeout),
                    strict_json_schema=False,
                )
            )
        except Exception as exc:
            logger.warning(f"[mcp-adapter] failed to wrap {agent_tool_name}: {exc}")
    return tools


__all__ = ["mcp_tools_to_function_tools"]
