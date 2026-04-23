"""Compatibility layer for migrating from agentic_go to OpenAI Agents SDK.

Provides:
- convert_tool(): wraps existing tool classes (with name/description/param_anno/__call__)
  into OpenAI Agents SDK FunctionTool instances
- BaseTool: minimal base class for tool definitions (replaces agentic_go.core.tool.BaseTool)
"""

from __future__ import annotations

import json
from typing import Any

from agents import FunctionTool
from agents.run_context import RunContextWrapper

from utils.logger import logger


class ToolResult:
    """Simple tool result container (replaces agentic_go.core.tool.ToolResult)."""

    def __init__(self, output: str = "", error: str = "", context_variables: dict = None):
        self.output = output
        self.error = error
        self.context_variables = context_variables or {}

    def __str__(self):
        return self.output if self.output else self.error


class BaseTool:
    """Minimal base class for tool definitions.

    Replaces agentic_go.core.tool.BaseTool. Tools subclass this and define:
    - name: str — tool identifier
    - description: str — LLM-visible description
    - param_anno — Pydantic BaseModel class for parameters
    - async __call__(self, params) -> str — execution method
    """

    name: str = ""
    description: str = ""
    param_anno = None

    def __init__(self, **kwargs):
        pass

    async def __call__(self, **kwargs):
        pass


def convert_tool(tool_instance: Any) -> FunctionTool:
    """Convert a tool instance (with name/description/param_anno/__call__)
    to an OpenAI Agents SDK FunctionTool.

    The tool's Pydantic param_anno is converted to JSON schema for the SDK.
    The tool's async __call__ is wrapped to accept SDK's (ctx, args_json) signature.
    """
    name = getattr(tool_instance, "name", "")
    description = getattr(tool_instance, "description", "")
    param_model = getattr(tool_instance, "param_anno", None)

    if not name or not param_model:
        raise ValueError(f"Tool must have name and param_anno: {tool_instance}")

    # Get JSON schema from Pydantic model
    schema = param_model.model_json_schema()

    async def _invoke(ctx: RunContextWrapper, args_json: str) -> str:
        """SDK callback: parse args JSON, call tool, return string result."""
        try:
            params_dict = json.loads(args_json) if isinstance(args_json, str) else args_json
            params = param_model(**params_dict)
            result = await tool_instance(params)
            # Handle ToolResult-like objects (e.g., fetch_url returns objects with .output)
            if hasattr(result, "output"):
                return str(result.output)
            return str(result) if result is not None else ""
        except Exception as e:
            logger.error(f"Tool {name} error: {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,  # Allow flexible schemas (optional fields, etc.)
    )


def convert_tools(tool_instances: list[Any]) -> list[FunctionTool]:
    """Batch convert tool instances to FunctionTool list."""
    result = []
    for t in tool_instances:
        try:
            result.append(convert_tool(t))
        except Exception as e:
            logger.warning(f"Failed to convert tool {getattr(t, 'name', '?')}: {e}")
    return result
