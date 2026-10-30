"""Deferred schemas and search-then-call behavior through the real SDK loop."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, FunctionTool, Model, RunConfig, Runner
from agents.items import ModelResponse
from agents.run_context import RunContextWrapper
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel

from configs import settings
from core.agent_compat import BaseTool
from core.tool_search import configure_agent_tools
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture(autouse=True)
def search_settings(monkeypatch):
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOLSEARCH_ALWAYS_LOADED", ("memory", "skill"))


def make_tool(name, description="A deferred business tool", *, enabled=True, calls=None):
    async def invoke(_ctx, arguments):
        if calls is not None:
            calls.append((name, json.loads(arguments)))
        return "business result"

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        on_invoke_tool=invoke,
        strict_json_schema=False,
        is_enabled=enabled,
    )


async def visible(agent, context=None):
    return {tool.name for tool in await agent.get_all_tools(RunContextWrapper(context=context))}


async def search(agent, query, limit=5, context=None):
    tool = next(tool for tool in agent.tools if tool.name == "toolsearch")
    result = await tool.on_invoke_tool(
        RunContextWrapper(context=context),
        json.dumps({"query": query, "limit": limit}),
    )
    return json.loads(result)


@pytest.mark.asyncio
async def test_only_resident_tools_expose_schemas_until_search():
    calls = []
    agent = Agent(
        name="test", tools=[make_tool("memory"), make_tool("execute_code", "Run bash Python", calls=calls)]
    )
    await configure_agent_tools(agent)
    assert await visible(agent) == {"toolsearch", "memory"}
    result = await search(agent, "python")
    assert [tool["name"] for tool in result["tools"]] == ["execute_code"]
    assert result["tools"][0]["newly_loaded"] is True
    assert "params_json_schema" not in result["tools"][0]
    assert await visible(agent) == {"toolsearch", "memory", "execute_code"}
    assert calls == []  # Discovery must never perform the business action.
    assert (await search(agent, "execute_code"))["tools"][0]["newly_loaded"] is False


@pytest.mark.asyncio
async def test_keyword_search_and_exact_name_priority_and_result_limit():
    agent = Agent(
        name="test",
        tools=[
            make_tool("image_generation", "Generate images from English prompts"),
            make_tool("image_editing", "Edit images and generate image variants"),
        ],
    )
    await configure_agent_tools(agent)
    results = await search(agent, "generate images", limit=1)
    assert len(results["tools"]) == 1
    assert results["tools"][0]["name"] == "image_generation"
    assert (await search(agent, "image_editing", limit=1))["tools"][0]["name"] == "image_editing"


@pytest.mark.asyncio
async def test_search_preserves_boolean_and_context_dependent_enablement():
    async def user_allowed(ctx, _agent):
        return ctx.context == "allowed"

    agent = Agent(
        name="test",
        tools=[
            make_tool("blocked", enabled=False),
            make_tool("private", enabled=user_allowed),
        ],
    )
    await configure_agent_tools(agent)
    assert not (await search(agent, "blocked"))["tools"]
    assert not (await search(agent, "private", context="denied"))["tools"]
    assert (await search(agent, "private", context="allowed"))["tools"]
    assert "private" in await visible(agent, "allowed")
    assert "private" not in await visible(agent, "denied")
    assert "blocked" not in await visible(agent, "allowed")


@pytest.mark.asyncio
async def test_lazy_discovery_is_once_for_parallel_searches_and_has_no_duplicates():
    attempts = []

    async def loader(existing):
        attempts.append(existing)
        await asyncio.sleep(0)
        return [make_tool("mcp__server__docs__lookup", "Look up documentation"), make_tool("memory")]

    agent = Agent(name="test", tools=[make_tool("memory")])
    await configure_agent_tools(agent, loader=loader)
    assert not attempts
    await asyncio.gather(search(agent, "lookup"), search(agent, "documentation"))
    assert len(attempts) == 1
    assert {"toolsearch", "memory"} <= attempts[0]
    assert [tool.name for tool in agent.tools].count("memory") == 1
    assert "mcp__server__docs__lookup" in await visible(agent)


@pytest.mark.asyncio
async def test_loader_failure_retains_local_search_and_can_retry():
    attempts = 0

    async def loader(_existing):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("unavailable")
        return [make_tool("mcp__server__docs__lookup")]

    agent = Agent(name="test", tools=[make_tool("execute_code", "Python bash")])
    await configure_agent_tools(agent, loader=loader)
    result = await search(agent, "bash")
    assert result["tools"] and "discovery_error" in result
    assert (await search(agent, "lookup"))["tools"][0]["source"] == "mcp"
    assert attempts == 2


@pytest.mark.asyncio
async def test_manager_retries_failed_server_but_caches_successful_and_empty_catalogs(monkeypatch):
    from contextlib import AsyncExitStack

    from core.mcp.http_consumer import HTTPMCPConsumer
    from core.mcp.manager import MCPToolManager

    counts = {"healthy": 0, "recovering": 0, "empty": 0}

    class WorkspaceConfig:
        def mcp_config_load(self):
            return {
                "servers": {name: {"transport": "http", "url": "https://example.com/mcp"} for name in counts}
            }

    async def open_session(consumer):
        name = consumer.server.name
        counts[name] += 1
        if name == "recovering" and counts[name] == 1:
            raise ConnectionError("temporary outage")
        tools = [] if name == "empty" else [{"name": "lookup", "description": "lookup documentation"}]
        return AsyncExitStack(), SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=tools))
        )

    monkeypatch.setattr(HTTPMCPConsumer, "_open_session", open_session)
    manager = MCPToolManager(user_id="alice", session_id="test", workspace=WorkspaceConfig())
    agent = Agent(name="test")
    await configure_agent_tools(agent, loader=manager.create_tools)
    first = await search(agent, "lookup")
    assert "discovery_error" in first
    assert {tool["name"] for tool in first["tools"]} == {"mcp__server__healthy__lookup"}
    second = await search(agent, "lookup")
    assert "discovery_error" not in second
    assert {tool["name"] for tool in second["tools"]} == {
        "mcp__server__healthy__lookup",
        "mcp__server__recovering__lookup",
    }
    await search(agent, "lookup")
    assert counts == {"healthy": 1, "recovering": 2, "empty": 1}
    assert len([tool for tool in agent.tools if tool.name == "mcp__server__healthy__lookup"]) == 1


@pytest.mark.asyncio
async def test_registry_read_failure_is_retryable_in_same_search_run():
    from core.mcp.manager import MCPToolManager

    class WorkspaceConfig:
        calls = 0

        def mcp_config_load(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("storage temporarily unavailable")
            return {}

    workspace = WorkspaceConfig()
    manager = MCPToolManager(user_id="alice", session_id="test", workspace=workspace)
    agent = Agent(name="test")
    await configure_agent_tools(agent, loader=manager.create_tools)
    assert "discovery_error" in await search(agent, "lookup")
    assert "discovery_error" not in await search(agent, "lookup")
    await search(agent, "lookup")
    assert workspace.calls == 2


@pytest.mark.asyncio
async def test_stdio_catalog_failure_propagates_for_retry(monkeypatch):
    import core.mcp.sandbox_stdio_consumer as consumer_module
    from core.mcp.registry import MCPServerConfig
    from core.mcp.sandbox_stdio_consumer import SandboxStdioMCPConsumer

    session = SimpleNamespace(list_tools=AsyncMock(side_effect=[ConnectionError("outage"), []]))
    monkeypatch.setattr(consumer_module, "get_sandbox_stdio_session", AsyncMock(return_value=session))
    consumer = SandboxStdioMCPConsumer(
        user_id="alice", session_id="test", server=MCPServerConfig(name="test", transport="stdio")
    )
    with pytest.raises(ConnectionError):
        await consumer.list_tools()
    assert await consumer.list_tools() == []


@pytest.mark.asyncio
async def test_explicit_mcp_requirement_preloads_only_that_tool():
    required = "mcp__server__docs__lookup"
    loader = AsyncMock(return_value=[make_tool(required), make_tool("mcp__server__docs__unrelated")])
    agent = Agent(name="test")
    await configure_agent_tools(agent, loader=loader, initially_loaded=[required])
    loader.assert_awaited_once()
    assert await visible(agent) == {"toolsearch", required}


@pytest.mark.asyncio
async def test_state_is_scoped_to_agent_and_required_tools_are_preloaded():
    agents = [
        Agent(name=name, tools=[make_tool("web_search"), make_tool("execute_code")])
        for name in ("alice", "bob")
    ]
    await configure_agent_tools(agents[0], initially_loaded=["websearch"])
    await configure_agent_tools(agents[1])
    assert "web_search" in await visible(agents[0])
    assert "web_search" not in await visible(agents[1])
    await search(agents[0], "execute_code")
    assert "execute_code" not in await visible(agents[1])


@pytest.mark.asyncio
async def test_no_match_and_unconfigured_tool_remain_hidden(monkeypatch):
    monkeypatch.setattr(settings, "TOOLSEARCH_ALWAYS_LOADED", ("execute_code",))
    agent = Agent(name="test", tools=[make_tool("web_search", "Search the web")])
    await configure_agent_tools(agent)
    assert not (await search(agent, "execute_code"))["tools"]
    assert await visible(agent) == {"toolsearch"}


@pytest.mark.asyncio
async def test_disabled_mode_preserves_eager_loading(monkeypatch):
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)

    async def loader(_existing):
        return [make_tool("mcp__server__docs__lookup")]

    agent = Agent(name="test", instructions="Original", tools=[make_tool("execute_code")])
    assert await configure_agent_tools(agent, loader=loader) is None
    assert await visible(agent) == {"execute_code", "mcp__server__docs__lookup"}
    assert agent.instructions == "Original"


@pytest.mark.asyncio
@pytest.mark.parametrize("query,limit", [(" ", 5), ("python", 0), ("python", 11)])
async def test_invalid_search_does_not_discover_tools(query, limit):
    async def loader(_existing):
        raise AssertionError("must not discover for invalid input")

    agent = Agent(name="test")
    await configure_agent_tools(agent, loader=loader)
    assert "error" in await search(agent, query, limit)


class SearchThenCallModel(Model):
    """Offline model asserting the schemas visible on each SDK model turn."""

    def __init__(self, name):
        self.target = name
        self.seen = []

    def response(self, tools, input):
        self.seen.append({tool.name for tool in tools})
        turn = len(self.seen)
        if turn == 1:
            assert self.target not in self.seen[-1]
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    call_id="search-call",
                    name="toolsearch",
                    arguments=json.dumps({"query": self.target, "limit": 1}),
                )
            ]
        elif turn == 2:
            assert self.target in self.seen[-1]
            target_tool = next(tool for tool in tools if tool.name == self.target)
            assert "value" in target_tool.params_json_schema["properties"]
            assert any(item.get("type") == "function_call_output" for item in input)
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    call_id="business-call",
                    name=self.target,
                    arguments='{"value":"test"}',
                )
            ]
        else:
            assert turn == 3
            assert any(item.get("output") == "business result" for item in input)
            output = [
                ResponseOutputMessage(
                    type="message",
                    id="message-final",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
                )
            ]
        return ModelResponse(output=output, usage=Usage(), response_id=f"response-{turn}")

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs
    ):
        return self.response(tools, input)

    async def stream_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs
    ):
        result = self.response(tools, input)
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=0,
            response=Response(
                id=result.response_id,
                created_at=0,
                model="offline",
                object="response",
                output=result.output,
                parallel_tool_calls=True,
                tool_choice="auto",
                tools=[],
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_sdk_search_then_call_works_in_same_run(streamed):
    calls = []
    model = SearchThenCallModel("mcp__server__docs__lookup")

    async def loader(_existing):
        return [make_tool(model.target, calls=calls)]

    agent = Agent(name="test", model=model, tools=[make_tool("unrelated")])
    await configure_agent_tools(agent, loader=loader)
    config = RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, input="look up documentation", run_config=config, max_turns=4)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, input="look up documentation", run_config=config, max_turns=4)
    assert result.final_output == "done"
    assert calls == [(model.target, {"value": "test"})]
    assert all("unrelated" not in names for names in model.seen)


@pytest.mark.asyncio
async def test_mcp_discovery_retains_registry_include_exclude_filters():
    from core.mcp.manager import MCPToolManager

    class Consumer:
        lists = 0

        async def list_tools(self):
            self.lists += 1
            return [
                {"name": name, "description": name, "inputSchema": {"type": "object"}}
                for name in ("read_public", "read_secret", "write_public")
            ]

    class WorkspaceConfig:
        def mcp_config_load(self):
            return {
                "servers": {
                    "docs": {
                        "enabled": True,
                        "transport": "http",
                        "url": "https://example.com/mcp",
                        "toolFilter": {"include": ["read_*"], "exclude": ["read_secret"]},
                    }
                }
            }

    consumer = Consumer()
    manager = MCPToolManager(user_id="alice", session_id="test", workspace=WorkspaceConfig())
    manager._consumer_for = lambda _server: consumer
    agent = Agent(name="test")
    await configure_agent_tools(agent, loader=manager.create_tools)
    assert consumer.lists == 0
    result = await search(agent, "public")
    assert [tool["name"] for tool in result["tools"]] == ["mcp__server__docs__read_public"]
    assert all(
        tool["name"] == "mcp__server__docs__read_public"
        for tool in (await search(agent, "read_secret"))["tools"]
    )
    assert all(
        tool["name"] == "mcp__server__docs__read_public"
        for tool in (await search(agent, "write_public"))["tools"]
    )
    assert consumer.lists == 1


@pytest.mark.asyncio
async def test_main_agent_factory_installs_discovery_and_runs_search_then_call(tmp_path, monkeypatch):
    import core.agent as core_agent

    calls = []

    class CodeParams(BaseModel):
        code: str

    class Builtin(BaseTool):
        name = "execute_code"
        description = "Run Python code"
        param_anno = CodeParams

        async def __call__(self, params):
            calls.append(params.code)
            return "business result"

    monkeypatch.setattr(core_agent, "_ensure_sdk_client", lambda: None)
    discover = AsyncMock(return_value=[])
    monkeypatch.setattr(core_agent.MCPToolManager, "create_tools", discover)
    orchestrator = core_agent.EvoAgent.__new__(core_agent.EvoAgent)
    orchestrator.user_id = "alice"
    orchestrator.session_id = "test"
    orchestrator.workspace = Workspace("alice", backend=LocalFSBackend(root=tmp_path))
    orchestrator.tools = [Builtin()]
    orchestrator.system_prompt = "Test instructions"
    orchestrator.model = "offline"
    orchestrator.matched_skill = None
    agent = await orchestrator._create_sdk_agent()
    assert await visible(agent) == {"toolsearch"}
    discover.assert_not_awaited()
    await search(agent, "execute_code")
    assert "execute_code" in await visible(agent)
    tool = next(tool for tool in agent.tools if tool.name == "execute_code")
    assert (
        await tool.on_invoke_tool(RunContextWrapper(context=None), '{"code":"print(1)"}') == "business result"
    )
    assert calls == ["print(1)"]


@pytest.mark.asyncio
async def test_sub_agent_stream_uses_discovery(tmp_path, monkeypatch):
    import core.sub_agent as sub_module

    sub_module._ensure_imports()
    monkeypatch.setattr(sub_module, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(sub_module, "OPENAI_API_KEY", "offline-test")
    monkeypatch.setattr(sub_module, "DEFAULT_SUB_AGENT_TOOLS", [])
    model = SearchThenCallModel("mcp__server__docs__lookup")
    calls = []
    monkeypatch.setattr(
        sub_module.MCPToolManager,
        "create_tools",
        AsyncMock(
            return_value=[
                make_tool(model.target, calls=calls),
            ]
        ),
    )
    monkeypatch.setattr(sub_module, "_SDKAgent", lambda **kwargs: Agent(**(kwargs | {"model": model})))
    monkeypatch.setattr(
        sub_module,
        "Runner",
        SimpleNamespace(
            run_streamed=lambda *args, **kwargs: Runner.run_streamed(
                *args,
                **kwargs,
                run_config=RunConfig(tracing_disabled=True),
            ),
        ),
    )
    runner = sub_module.SubAgentRunner(
        sub_module.AgentTypeConfig(name="test", max_steps=4),
        user_id="alice",
        session_id="test",
        workspace=Workspace("alice", backend=LocalFSBackend(root=tmp_path)),
    )
    monkeypatch.setattr(runner, "_check_completion", AsyncMock(return_value=True))
    events = [event async for event in runner.run_stream("lookup documentation")]
    tool_calls = [json.loads(event["content"])["name"] for event in events if event["code"] == 4]
    assert tool_calls == ["toolsearch", model.target]
    assert calls == [(model.target, {"value": "test"})]
