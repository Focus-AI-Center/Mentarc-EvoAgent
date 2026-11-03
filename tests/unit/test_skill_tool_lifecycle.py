"""Guide restoration, dynamic SDK schemas and compression recovery, offline."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, RunConfig, Runner
from agents.run_context import RunContextWrapper
from pydantic import BaseModel

from configs import settings
from core.agent import _history_to_sdk_input
from core.agent_compat import BaseTool, convert_tools
from core.plugins.internal import SkillParams, SkillTool
from core.skills import Skill
from core.skills.manager import SkillManager
from core.skills.tool_mounting import SkillToolMounter
from core.tool_search import configure_agent_tools
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from utils.history_compressor import HistoryCompressor
from utils.skill_history import loaded_skill_names
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


class Params(BaseModel):
    value: str = "test"


class BusinessTool(BaseTool):
    name = "business"
    description = "Execute the guided workflow"
    param_anno = Params

    def __init__(self, **kwargs):
        self.identity = kwargs

    async def __call__(self, params):
        return "business completed"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import core.skills.tool_mounting as mounting

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOLSEARCH_ALWAYS_LOADED", ("skill",))
    monkeypatch.setattr(
        mounting, "initialize_plugins", lambda names, **kwargs: [BusinessTool(**kwargs) for _ in names]
    )
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    manager = SkillManager(workspace)
    skill = Skill(
        name="workflow",
        description="A guided workflow",
        instructions="ORIGINAL_GUIDE " * 1000,
        required_tools=["business"],
        references={"rules.md": "EXACT_REFERENCE " * 1000},
    )
    manager.save_user_skill("alice", skill)
    tool = SkillTool(user_id="alice", session_id="session-a", skill_manager=manager, workspace=workspace)
    tools = [tool]
    mounter = SkillToolMounter(tools, user_id="alice", session_id="session-a", workspace=workspace)
    return SimpleNamespace(workspace=workspace, tool=tool, tools=tools, mounter=mounter)


def pair(body, *, action="get", call_id="load"):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {
                        "name": "skill",
                        "arguments": json.dumps(
                            {"action": action, "name": "workflow", "ref_filename": "rules.md"}
                        ),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "name": "skill", "content": body},
    ]


async def visible(agent):
    return {tool.name for tool in await agent.get_all_tools(RunContextWrapper(context=None))}


@pytest.mark.asyncio
@pytest.mark.parametrize("search_enabled", [True, False])
async def test_get_mounts_tools_for_next_sdk_call_and_guide_survives_reload(
    runtime, monkeypatch, search_enabled
):
    from openai.types.responses import ResponseFunctionToolCall

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", search_enabled)

    def script(turn, tools, input):
        names = {tool.name for tool in tools}
        if turn == 1:
            assert "business" not in names
            return [
                ResponseFunctionToolCall(
                    type="function_call",
                    call_id="guide",
                    name="skill",
                    arguments='{"action":"get","name":"workflow"}',
                )
            ]
        if turn == 2:
            assert "business" in names
            assert any("ORIGINAL_GUIDE" in item.get("output", "") for item in input)
            return [
                ResponseFunctionToolCall(
                    type="function_call", call_id="execute", name="business", arguments="{}"
                )
            ]
        assert any(item.get("output") == "business completed" for item in input)
        return [final_message()]

    agent = Agent(name="worker", model=ScriptedModel(script), tools=convert_tools(runtime.tools))
    catalog = await configure_agent_tools(agent)
    await runtime.mounter.bind(agent, catalog)
    result = await Runner.run(agent, input="run workflow", run_config=RunConfig(tracing_disabled=True))
    assert result.final_output == "done"
    business = next(tool for tool in runtime.tools if tool.name == "business")
    assert business.identity["session_id"] == "session-a"
    from core.agent import _sdk_result_to_history

    history = _sdk_result_to_history([], result)
    assert loaded_skill_names(history) == ["workflow"]
    assert "ORIGINAL_GUIDE" in json.dumps(_history_to_sdk_input(history))
    fresh_tools = [
        SkillTool(user_id="alice", skill_manager=SkillManager(runtime.workspace), workspace=runtime.workspace)
    ]
    fresh = SkillToolMounter(
        fresh_tools, user_id="alice", session_id="session-a", workspace=runtime.workspace
    )
    await fresh.restore(history)
    assert "business" in {tool.name for tool in fresh_tools}


@pytest.mark.asyncio
async def test_reference_first_load_includes_main_guide_and_failed_get_does_not_mount(runtime):
    missing = await runtime.tool(SkillParams(action="get", name="missing"))
    assert "does not exist" in missing
    assert not runtime.mounter.loaded and len(runtime.tools) == 1
    reference = await runtime.tool(SkillParams(action="get_ref", name="workflow", ref_filename="rules.md"))
    assert "ORIGINAL_GUIDE" in reference and "EXACT_REFERENCE" in reference
    assert runtime.mounter.loaded == {"workflow"}
    assert loaded_skill_names(pair(reference, action="get_ref")) == ["workflow"]
    assert loaded_skill_names(pair("Skill 'workflow' does not exist.")) == []
    assert loaded_skill_names([{"role": "assistant", "content": "Previously loaded workflow"}]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["light", "moderate", "aggressive"])
async def test_compression_unloads_tools_and_dedup_then_reload_restores_exact_guides(runtime, level):
    agent = Agent(name="worker", tools=convert_tools(runtime.tools))
    catalog = await configure_agent_tools(agent)
    await runtime.mounter.bind(agent, catalog)
    guide = await runtime.tool(SkillParams(action="get", name="workflow"))
    reference = await runtime.tool(SkillParams(action="get_ref", name="workflow", ref_filename="rules.md"))
    history = (
        [{"role": "user", "content": "first"}]
        + pair(guide)
        + pair(reference, action="get_ref", call_id="ref")
    )
    history += [{"role": "user", "content": "continue"}]
    compressor = HistoryCompressor(
        max_history_tokens=1, compression_level=level, min_messages_to_keep=1, async_client=SimpleNamespace()
    )

    async def summarize(messages):
        assert "ORIGINAL_GUIDE" not in json.dumps(messages)
        assert "EXACT_REFERENCE" not in json.dumps(messages)
        return "Continue the workflow; reload its original instructions."

    compressor.summarize_conversation_segment = summarize
    compressor.compaction_callbacks.append(runtime.mounter.on_context_compacted)
    compressed = await compressor.compress_history(history)
    assert "ORIGINAL_GUIDE" not in json.dumps(compressed)
    assert "EXACT_REFERENCE" not in json.dumps(compressed)
    assert "business" not in await visible(agent)
    assert not runtime.tool._got_skills
    assert loaded_skill_names(compressed) == []
    original = await runtime.tool(SkillParams(action="get", name="workflow"))
    assert original == guide
    assert "business" in await visible(agent)
    assert reference == await runtime.tool(
        SkillParams(action="get_ref", name="workflow", ref_filename="rules.md")
    )


@pytest.mark.asyncio
async def test_compaction_preserves_resident_tools_and_search_grants(runtime):
    resident = BusinessTool()
    runtime.tools.append(resident)
    agent = Agent(name="worker", tools=convert_tools(runtime.tools))
    catalog = await configure_agent_tools(agent, initially_loaded=["business"])
    await runtime.mounter.bind(agent, catalog)
    await runtime.tool(SkillParams(action="get", name="workflow"))
    runtime.mounter.on_context_compacted()
    assert resident in runtime.tools and "business" in await visible(agent)


@pytest.mark.asyncio
async def test_disabled_compression_and_summary_failure_preserve_guides_and_tools(runtime):
    guide = await runtime.tool(SkillParams(action="get", name="workflow"))
    history = [{"role": "user", "content": "first"}] + pair(guide) + [{"role": "user", "content": "second"}]
    for level in ("none", "moderate"):
        compressor = HistoryCompressor(
            max_history_tokens=1,
            compression_level=level,
            min_messages_to_keep=1,
            async_client=SimpleNamespace(),
        )
        compressor.summarize_conversation_segment = AsyncMock(
            return_value="Summary generation failed: offline"
        )
        callback = AsyncMock()
        compressor.compaction_callbacks.append(callback)
        assert await compressor.compress_history(history) is history
        callback.assert_not_called()
    assert runtime.mounter.loaded == {"workflow"} and "workflow" in runtime.tool._got_skills


def test_restored_sdk_input_keeps_tool_pairs_and_excludes_display_metadata():
    history = pair("Skill document `workflow/SKILL.md`:\nEXACT_GUIDE")
    history[-1]["delivery_events"] = [{"code": 2, "content": "display only"}]
    history += pair("Another result", call_id="load")
    restored = _history_to_sdk_input(history)
    calls = [item["call_id"] for item in restored if item.get("type") == "function_call"]
    outputs = [item["call_id"] for item in restored if item.get("type") == "function_call_output"]
    assert calls == outputs and len(set(calls)) == 2
    assert "EXACT_GUIDE" in json.dumps(restored) and "display only" not in json.dumps(restored)


@pytest.mark.asyncio
async def test_compaction_retains_task_receipts_without_llm_rewriting_them():
    receipt = {
        "type": "sub_agent_receipt",
        "task_id": "report-a",
        "owner": "worker",
        "status": "waiting_user",
    }
    history = [
        {"role": "user", "content": "first " + "Evidence and facts. " * 500},
        {"role": "tool", "content": json.dumps(receipt)},
        {"role": "user", "content": "second"},
    ]
    compressor = HistoryCompressor(
        max_history_tokens=1, min_messages_to_keep=1, async_client=SimpleNamespace()
    )
    compressor.summarize_conversation_segment = AsyncMock(return_value="Summary")
    compressed = await compressor.compress_history(history)
    assert json.dumps(receipt) in compressed[0]["content"]


@pytest.mark.asyncio
async def test_repeated_compression_retains_receipt_ids_and_skill_reload_pointers(runtime):
    guide = await runtime.tool(SkillParams(action="get", name="workflow"))
    receipt = {
        "type": "sub_agent_receipt",
        "task_id": "keep-this-id",
        "owner": "worker",
        "status": "waiting_user",
    }
    history = [{"role": "user", "content": "first"}] + pair(guide)
    history += [{"role": "tool", "content": json.dumps(receipt)}, {"role": "user", "content": "second"}]
    compressor = HistoryCompressor(
        max_history_tokens=1, min_messages_to_keep=1, async_client=SimpleNamespace()
    )
    compressor.summarize_conversation_segment = AsyncMock(return_value="A deliberately minimal summary")
    first = await compressor.compress_history(history)
    second = await compressor.compress_history(first + [{"role": "user", "content": "third"}])
    assert json.dumps(receipt) in second[0]["content"]
    assert "workflow" in second[0]["content"]
    assert loaded_skill_names(second) == []
    assert compressor.delegation_receipts(second) == [receipt]


@pytest.mark.asyncio
async def test_skill_mcp_grant_is_lazy_and_revoked_without_removing_configured_catalog(runtime):
    from tests.unit.test_tool_search import make_tool

    required = "mcp__server__docs__lookup"
    manager = SkillManager(runtime.workspace)
    manager.save_user_skill(
        "alice",
        Skill(name="mcp-guide", description="MCP guide", instructions="Read docs", required_tools=[required]),
    )
    loader = AsyncMock(return_value=[make_tool(required), make_tool("mcp__server__docs__unrelated")])
    agent = Agent(name="worker", tools=convert_tools(runtime.tools))
    catalog = await configure_agent_tools(agent, loader=loader)
    await runtime.mounter.bind(agent, catalog)
    loader.assert_not_awaited()
    await runtime.tool(SkillParams(action="get", name="mcp-guide"))
    loader.assert_awaited_once()
    assert required in await visible(agent)
    assert "mcp__server__docs__unrelated" not in await visible(agent)
    runtime.mounter.on_context_compacted()
    assert required not in await visible(agent)
    assert required in catalog.names


@pytest.mark.asyncio
async def test_configured_child_skill_mounts_tools_and_history_is_saved_before_end(runtime, monkeypatch):
    import core.sub_agent as module

    monkeypatch.setattr(module, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(module, "DEFAULT_SUB_AGENT_TOOLS", [])
    monkeypatch.setattr(module.MCPToolManager, "create_tools", AsyncMock(return_value=[]))

    def script(turn, tools, input):
        assert "business" in {tool.name for tool in tools}
        assert any("ORIGINAL_GUIDE" in item.get("output", "") for item in input)
        return [final_message("Workflow complete")]

    monkeypatch.setattr(
        module, "_SDKAgent", lambda **kwargs: Agent(**(kwargs | {"model": ScriptedModel(script)}))
    )
    monkeypatch.setattr(
        module,
        "Runner",
        SimpleNamespace(
            run_streamed=lambda *args, **kwargs: Runner.run_streamed(
                *args, **kwargs, run_config=RunConfig(tracing_disabled=True)
            )
        ),
    )
    config = module.AgentTypeConfig(name="configured", skills=["workflow"])
    for _ in range(2):
        child = module.SubAgentRunner(
            config, user_id="alice", session_id="thread", workspace=runtime.workspace
        )
        async for event in child.run_stream("run"):
            if event.get("content") == "stream_end":
                saved = runtime.workspace.history_load(child.session_id)
                assert saved and saved["conversation"][-1]["content"] == "Workflow complete"
    from utils.skill_history import skill_loads

    assert len(list(skill_loads(saved["conversation"]))) == 1
