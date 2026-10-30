"""Request-scoped tool discovery with deferred SDK function schemas."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace

from agents import Agent, FunctionTool
from agents.run_context import RunContextWrapper
from pydantic import BaseModel, Field

from configs import settings
from utils.logger import logger
from utils.tool_names import canonical_tool_name

ToolLoader = Callable[[set[str]], Awaitable[list[FunctionTool]]]
TOOLSEARCH_GUIDANCE = """
## Tool search
Some enabled tool schemas are loaded on demand. If a needed tool is absent, first call
toolsearch(query="tool name or capability", limit=5). Search enabled plugins and MCP tools
by name, description or service name. Search only loads definitions; it does not execute
business operations. Wait for the result, then call a matched tool in a later model step.
Do not search and invoke a deferred tool in the same parallel step. Matched tools remain
available during this run. Try another query when needed; do not invent tool names.
Search does not enable plugins or MCP services that are absent from the configuration.
""".strip()


class ToolSearchParams(BaseModel):
    query: str = Field(
        min_length=1, max_length=512, description="Tool name, MCP service name or capability keywords"
    )
    limit: int = Field(default=5, ge=1, le=10, description="Maximum results to load: default 5, at most 10")


def _name_key(name: str) -> str:
    return canonical_tool_name(name).casefold().removesuffix("tool").replace("_", "").replace("-", "")


def _terms(query: str) -> set[str]:
    stop = {"the", "a", "an", "to", "for", "please", "tool", "tools", "use", "call", "need"}
    terms = set(re.findall(r"[a-z0-9]+", query.casefold()))
    for word in re.findall(r"[\u3400-\u9fff]+", query):
        terms.add(word)
        terms.update(word[i : i + 2] for i in range(len(word) - 1))
    return terms - stop


def _score(query: str, tool: FunctionTool) -> int:
    name, description = tool.name.casefold(), tool.description.casefold()
    score = 1000 if _name_key(query) == _name_key(name) else 0
    if query.casefold() in name:
        score += 40
    if query.casefold() in description:
        score += 10
    name_terms = set(re.findall(r"[a-z0-9]+", name))
    for term in _terms(query):
        if term in name_terms:
            score += 12
        elif term in name:
            score += 6
        if term in description:
            score += 2
    return score


class ToolSearchCatalog:
    """Keep schemas on the server until a search enables them for this run."""

    def __init__(self, agent: Agent, *, initially_loaded: Iterable[str], loader: ToolLoader | None = None):
        self.agent = agent
        self._initial_keys = {_name_key(name) for name in initially_loaded}
        self._active: set[str] = set()
        self._originals: dict[str, FunctionTool] = {}
        self._loader = loader
        self._discovered = loader is None
        self._lock = asyncio.Lock()
        self.tools = []
        self._add(agent.tools)
        self.tools.insert(
            0,
            FunctionTool(
                name="toolsearch",
                description=(
                    "Search enabled tools by name, MCP service or capability and load matching schemas. "
                    "Search does not execute tools. Call matches in a later step of this run."
                ),
                params_json_schema=ToolSearchParams.model_json_schema(),
                on_invoke_tool=self._invoke,
                strict_json_schema=False,
            ),
        )

    @property
    def names(self) -> set[str]:
        return {"toolsearch", *self._originals}

    def _add(self, tools: Iterable) -> None:
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                self.tools.append(tool)
                continue
            if tool.name == "toolsearch" or tool.name in self._originals:
                logger.warning(f"[toolsearch] duplicate tool skipped: {tool.name}")
                continue
            self._originals[tool.name] = tool
            if _name_key(tool.name) in self._initial_keys:
                self._active.add(tool.name)
            self.tools.append(replace(tool, is_enabled=self._gate(tool)))

    def _gate(self, tool: FunctionTool):
        async def enabled(ctx, _agent):
            return tool.name in self._active and await self._allowed(tool, ctx)

        return enabled

    async def _allowed(self, tool: FunctionTool, ctx: RunContextWrapper) -> bool:
        predicate = tool.is_enabled
        if isinstance(predicate, bool):
            return predicate
        result = predicate(ctx, self.agent)
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    async def discover(self) -> str | None:
        """Cache successful catalogs; failed discovery remains retryable."""
        if self._discovered:
            return None
        try:
            tools = await self._loader(self.names)
            self._add(tools)
            self._discovered = getattr(tools, "discovery_complete", True)
            if not self._discovered:
                return "Some MCP catalogs failed to load; retry later. Loaded tools remain searchable."
        except Exception:
            logger.exception("[toolsearch] tool catalog discovery failed")
            return "MCP catalog discovery failed; retry later. Local tools remain searchable."
        return None

    async def _invoke(self, ctx: RunContextWrapper, args_json: str) -> str:
        try:
            params = ToolSearchParams.model_validate_json(args_json)
        except ValueError:
            return json.dumps(
                {"error": "query must be nonempty and limit must be from 1 to 10."}, ensure_ascii=False
            )
        query = params.query.strip()
        if not query:
            return json.dumps({"error": "query cannot contain only whitespace."}, ensure_ascii=False)
        async with self._lock:
            discovery_error = await self.discover()
            ranked = []
            for tool in self._originals.values():
                score = _score(query, tool)
                if score > 0 and await self._allowed(tool, ctx):
                    ranked.append((score, tool))
            ranked.sort(key=lambda item: (-item[0], len(item[1].description), item[1].name))
            matches = []
            for _, tool in ranked[: params.limit]:
                newly_loaded = tool.name not in self._active
                self._active.add(tool.name)
                matches.append(
                    {
                        "name": tool.name,
                        "description": tool.description[:600],
                        "source": "mcp" if tool.name.startswith("mcp__") else "builtin",
                        "loaded": True,
                        "newly_loaded": newly_loaded,
                    }
                )
            payload = {
                "query": query,
                "tools": matches,
                "message": (
                    "Matching tool schemas will be available in the next model call; you may then invoke them."
                    if matches
                    else "No enabled tools matched. Try a tool name or more specific capability keywords."
                ),
            }
            if discovery_error:
                payload["discovery_error"] = discovery_error
            return json.dumps(payload, ensure_ascii=False)


async def configure_agent_tools(
    agent: Agent, *, loader: ToolLoader | None = None, initially_loaded: Iterable[str] = ()
) -> ToolSearchCatalog | None:
    """Install discovery, or preserve eager loading when disabled in settings."""
    if not settings.TOOLSEARCH_ENABLED:
        if loader is not None:
            agent.tools.extend(await loader({tool.name for tool in agent.tools}))
        return None
    preload = (*settings.TOOLSEARCH_ALWAYS_LOADED, *initially_loaded)
    catalog = ToolSearchCatalog(agent, initially_loaded=preload, loader=loader)
    agent.tools = catalog.tools
    if any(name.startswith("mcp__") for name in preload):
        await catalog.discover()
    original = agent.instructions
    if callable(original):

        async def instructions(ctx, current_agent):
            result = original(ctx, current_agent)
            text = await result if inspect.isawaitable(result) else result
            return f"{text or ''}\n\n{TOOLSEARCH_GUIDANCE}"

        agent.instructions = instructions
    else:
        agent.instructions = f"{original or ''}\n\n{TOOLSEARCH_GUIDANCE}"
    return catalog
