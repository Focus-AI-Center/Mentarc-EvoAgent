"""Request-scoped tool discovery with deferred SDK function schemas."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from typing import Annotated

from agents import Agent, FunctionTool
from agents.run_context import RunContextWrapper
from pydantic import BaseModel, Field, model_validator

from configs import settings
from core.tool_search_ranking import BM25Index, FieldIndex, normalize
from utils.logger import logger
from utils.tool_names import canonical_tool_name

ToolLoader = Callable[[set[str]], Awaitable[list[FunctionTool]]]
TOOLSEARCH_GUIDANCE = """
## Tool search
Some enabled tool schemas are loaded on demand. If a needed tool is absent, first call
toolsearch(query="tool name or capability", limit=5). Search enabled plugins and MCP tools
by name, description or service name. Search only loads definitions; it does not execute
business operations. Wait for the result, then call a matched tool in a later model step.
When you know exact tool names, use toolsearch(names=["exact_name"]) instead of query.
Names mode never loads approximate matches. Only rows with loaded=true received a search
grant; read failure reasons and suggestions. Schema budgets limit new search grants;
schemas are never truncated to fit. Do not repeatedly retry a full run budget.
Do not search and invoke a deferred tool in the same parallel step. Matched tools remain
available during this run. Try another query when needed; do not invent tool names.
Search does not enable plugins or MCP services that are absent from the configuration.
""".strip()


class ToolSearchParams(BaseModel):
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Tool name, MCP service name or capability keywords; use query or names, not both",
    )
    names: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="Exact tool names to load without fuzzy substitution; up to 10, instead of query",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum query results: default 5, at most 10; names mode processes the provided names",
    )

    @model_validator(mode="after")
    def validate_mode(self):
        if (self.query is None) == (self.names is None):
            raise ValueError("Provide exactly one of query or names.")
        if self.query is not None:
            self.query = self.query.strip()
            if not self.query:
                raise ValueError("query cannot contain only whitespace.")
        if self.names is not None:
            self.names = [name.strip() for name in self.names]
            if not all(self.names):
                raise ValueError("Tool names cannot contain only whitespace.")
        return self


def _exact_key(name: str) -> str:
    return canonical_tool_name(normalize(name))


def _name_key(name: str) -> str:
    return _exact_key(name).removesuffix("tool").replace("_", "").replace("-", "")


def schema_chars(tool: FunctionTool) -> int:
    """Measure a complete function definition in Unicode characters, not tokens."""
    definition = {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.params_json_schema,
        "strict": tool.strict_json_schema,
    }
    return len(json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class ToolSearchCatalog:
    """Keep schemas on the server until a search enables them for this run."""

    def __init__(
        self, agent: Agent, *, initially_loaded: Iterable[str], loader: ToolLoader | None = None, policy=None
    ):
        self.agent = agent
        self.policy = policy
        self.search_limit = settings.TOOLSEARCH_SEARCH_SCHEMA_CHARS
        self.active_limit = settings.TOOLSEARCH_ACTIVE_SCHEMA_CHARS
        if self.search_limit < 1 or self.active_limit < 1:
            raise ValueError("Tool Search schema character budgets must be positive.")
        self._initial_keys = {_name_key(name) for name in initially_loaded}
        self._resident: set[str] = set()
        self._active: set[str] = set()
        self._skill_active: set[str] = set()
        self._originals: dict[str, FunctionTool] = {}
        self._fields: dict[str, FieldIndex] = {}
        self._sizes: dict[str, int] = {}
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
                    "Use query for capability search or names for exact loading, not both. "
                    "Names never load approximate substitutes. Only loaded=true rows received a search grant. "
                    "New grants have per-search and cumulative schema character budgets. "
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
        if self.policy:
            tools = self.policy.filter(tools)
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                self.tools.append(tool)
                continue
            if tool.name == "toolsearch" or tool.name in self._originals:
                logger.warning(f"[toolsearch] duplicate tool skipped: {tool.name}")
                continue
            fields = FieldIndex.build(
                tool.name, tool.description, json.dumps(tool.params_json_schema, ensure_ascii=False)
            )
            size = schema_chars(tool)
            self._originals[tool.name] = tool
            self._fields[tool.name] = fields
            self._sizes[tool.name] = size
            if _name_key(tool.name) in self._initial_keys:
                self._active.add(tool.name)
                self._resident.add(tool.name)
            self.tools.append(replace(tool, is_enabled=self._gate(tool)))

    def _gate(self, tool: FunctionTool):
        async def enabled(ctx, _agent):
            return tool.name in (self._active | self._skill_active) and await self._allowed(tool, ctx)

        return enabled

    async def mount_skill_tools(self, names: Iterable[str], tools: Iterable = ()) -> None:
        self._add(tools)
        keys = {_name_key(name) for name in names}
        if any(name.startswith("mcp__") for name in names):
            await self.discover()
        self._skill_active.update(name for name in self._originals if _name_key(name) in keys)

    def unmount_skill_tools(self, owned_names: set[str]) -> None:
        self._skill_active.clear()
        for name in owned_names:
            self._originals.pop(name, None)
            self._active.discard(name)
            self._resident.discard(name)
            self._fields.pop(name, None)
            self._sizes.pop(name, None)
        self.tools[:] = [tool for tool in self.tools if tool.name not in owned_names]

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

    def _rank(self, query: str, tools: list[FunctionTool]) -> list[FunctionTool]:
        scores = BM25Index([self._fields[tool.name] for tool in tools]).scores(query)
        rows = [
            (_name_key(query) == _name_key(tool.name), score, tool)
            for tool, score in zip(tools, scores, strict=True)
            if score > 0 or _name_key(query) == _name_key(tool.name)
        ]
        rows.sort(key=lambda row: (not row[0], -row[1], row[2].name))
        return [tool for _, _, tool in rows]

    def _by_names(self, names: list[str], tools: list[FunctionTool]):
        candidates, missing, seen = [], [], set()
        for name in dict.fromkeys(names):
            exact = [tool for tool in tools if tool.name == name]
            matches = exact or [tool for tool in tools if _exact_key(tool.name) == _exact_key(name)]
            if len(matches) != 1:
                missing.append(
                    {
                        "name": name,
                        "loaded": False,
                        "newly_loaded": False,
                        "reason": "not_found" if not matches else "ambiguous_name",
                        "message": "No unique enabled tool matched this exact name; no substitute was loaded.",
                        "suggestions": [tool.name for tool in (matches or self._rank(name, tools))[:3]],
                    }
                )
            elif matches[0].name not in seen:
                seen.add(matches[0].name)
                candidates.append(matches[0])
        return candidates, missing

    def _load(self, tools: list[FunctionTool]):
        results, used = [], 0
        active = sum(self._sizes[name] for name in self._active - self._resident)
        for tool in tools:
            size = self._sizes[tool.name]
            newly_loaded = tool.name not in self._active
            reason = None
            if newly_loaded:
                if size > self.search_limit:
                    reason = "schema_too_large"
                elif used + size > self.search_limit:
                    reason = "search_schema_budget"
                elif active + size > self.active_limit:
                    reason = "active_schema_budget"
                else:
                    self._active.add(tool.name)
                    used += size
                    active += size
            row = {
                "name": tool.name,
                "description": tool.description[:600],
                "source": "mcp" if tool.name.startswith("mcp__") else "builtin",
                "loaded": reason is None,
                "newly_loaded": newly_loaded and reason is None,
                "schema_chars": size,
            }
            if reason:
                row["reason"] = reason
                row["message"] = {
                    "schema_too_large": "The full tool definition exceeds the single-search budget; simplify its definition.",
                    "search_schema_budget": "This call would exceed its schema budget; retry with fewer exact names.",
                    "active_schema_budget": "New search grants would exceed this run's cumulative schema budget.",
                }[reason]
            results.append(row)
        return results, {
            "unit": "characters",
            "search_limit": self.search_limit,
            "search_used": used,
            "active_limit": self.active_limit,
            "active_used": active,
        }

    async def _invoke(self, ctx: RunContextWrapper, args_json: str) -> str:
        try:
            params = ToolSearchParams.model_validate_json(args_json)
        except ValueError:
            return json.dumps(
                {
                    "error": "Provide either a nonempty query (up to 512 characters) or 1-10 exact names (1-200 characters each), with limit from 1 to 10."
                },
                ensure_ascii=False,
            )
        async with self._lock:
            discovery_error = await self.discover()
            eligible = [tool for tool in list(self._originals.values()) if await self._allowed(tool, ctx)]
            if params.names is not None:
                candidates, missing = self._by_names(params.names, eligible)
                has_more = False
            else:
                ranked = self._rank(params.query, eligible)
                candidates, missing = ranked[: params.limit], []
                has_more = len(ranked) > params.limit
            matches, budget = self._load(candidates)
            payload = {
                **({"query": params.query} if params.names is None else {"names": params.names}),
                "tools": matches + missing,
                "has_more": has_more,
                "schema_budget": budget,
                "message": (
                    "Tools with loaded=true will be available in the next model call. Inspect failure reasons for other rows."
                    if matches or missing
                    else "No enabled tools matched. Try a tool name or more specific capability keywords."
                ),
            }
            if discovery_error:
                payload["discovery_error"] = discovery_error
            return json.dumps(payload, ensure_ascii=False)


async def configure_agent_tools(
    agent: Agent, *, loader: ToolLoader | None = None, initially_loaded: Iterable[str] = (), policy=None
) -> ToolSearchCatalog | None:
    """Install discovery, or preserve eager loading when disabled in settings."""
    if not settings.TOOLSEARCH_ENABLED:
        if loader is not None:
            agent.tools.extend(await loader({tool.name for tool in agent.tools}))
        if policy:
            agent.tools = policy.filter(agent.tools)
        return None
    preload = (*settings.TOOLSEARCH_ALWAYS_LOADED, *initially_loaded)
    catalog = ToolSearchCatalog(agent, initially_loaded=preload, loader=loader, policy=policy)
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
