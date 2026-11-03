"""Real SDK-loop acceptance for compaction, tool grants and persistence."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import RunConfig, Runner
from openai.types.responses import ResponseFunctionToolCall

from configs import settings
from core.agent import EvoAgent, _history_to_sdk_input, _stream_sdk_events
from core.agent_compat import BaseTool, convert_tools
from core.context_compaction import CompactingAgent, SDKContextCompaction
from core.plugins.internal import SkillTool
from core.skills import Skill
from core.skills.manager import SkillManager
from core.skills.tool_mounting import SkillToolMounter
from core.tool_search import configure_agent_tools
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from tests.unit.test_skill_tool_lifecycle import Params
from utils.history_compressor import CompressionUnavailable, HistoryCompressor
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


class HugeTool(BaseTool):
    name = "huge"
    description = "Return a large result"
    param_anno = Params

    def __init__(self, **kwargs):
        self.calls = 0

    async def __call__(self, params):
        self.calls += 1
        return json.dumps(
            {
                "details_url": f"https://example.com/result-{self.calls}.json",
                "evidence": "large fact " * 4000 + "LATE_EVIDENCE",
            }
        )


def compactor():
    return HistoryCompressor(
        max_history_tokens=1500,
        compression_threshold=1,
        min_messages_to_keep=0,
        async_client=SimpleNamespace(),
    )


def call(name, call_id, arguments=None):
    return ResponseFunctionToolCall(
        type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments or {})
    )


@pytest.mark.asyncio
async def test_mid_turn_compaction_unloads_skill_tools_before_next_schema_snapshot(tmp_path, monkeypatch):
    import core.skills.tool_mounting as mounting

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOLSEARCH_ALWAYS_LOADED", ("skill",))
    huge = HugeTool()
    monkeypatch.setattr(mounting, "initialize_plugins", lambda names, **kwargs: [huge] if names else [])
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    manager = SkillManager(workspace)
    manager.save_user_skill(
        "alice",
        Skill(
            name="workflow", description="Workflow", instructions="EXACT_GUIDE " * 30, required_tools=["huge"]
        ),
    )
    skill = SkillTool(user_id="alice", skill_manager=manager, workspace=workspace)
    tools = [skill]
    mounter = SkillToolMounter(tools, user_id="alice", session_id="thread", workspace=workspace)
    comp = compactor()
    comp.compaction_callbacks.append(mounter.on_context_compacted)

    async def summarize(messages):
        assert "LATE_EVIDENCE" in json.dumps(messages)
        assert "EXACT_GUIDE" not in json.dumps(messages)
        return "The huge tool completed once. Reload workflow before finishing. Do not repeat the tool."

    comp.summarize_conversation_segment = summarize

    def script(turn, visible, input):
        names = {tool.name for tool in visible}
        if turn == 1:
            return [call("skill", "load", {"action": "get", "name": "workflow"})]
        if turn == 2:
            assert "huge" in names and "EXACT_GUIDE" in json.dumps(input)
            return [call("huge", "execute")]
        if turn == 3:
            assert "huge" not in names
            assert "<summary>" in json.dumps(input) and "EXACT_GUIDE" not in json.dumps(input)
            return [call("skill", "reload", {"action": "get", "name": "workflow"})]
        assert turn == 4 and "huge" in names and "EXACT_GUIDE" in json.dumps(input)
        return [final_message("Completed without repeating the tool.")]

    agent = CompactingAgent(name="worker", model=ScriptedModel(script), tools=convert_tools(tools))
    catalog = await configure_agent_tools(agent)
    await mounter.bind(agent, catalog)
    history = [{"role": "user", "content": "Run workflow"}]
    controller = SDKContextCompaction(agent, comp, history, tools)
    result = Runner.run_streamed(
        agent, input=_history_to_sdk_input(history), run_config=RunConfig(tracing_disabled=True)
    )
    controller.bind(result)
    events = [event async for event in _stream_sdk_events(result, tools)]
    assert huge.calls == 1 and comp.compression_count == 1
    assert len(result.raw_responses) == 4
    assert [json.loads(event["content"])["name"] for event in events if event["code"] == 4] == [
        "skill",
        "huge",
        "skill",
    ]
    saved = controller.snapshot()
    assert saved[0]["content"].startswith("<summary>")
    assert saved[-1]["content"] == "Completed without repeating the tool."
    assert "LATE_EVIDENCE" not in json.dumps(saved)


@pytest.mark.asyncio
async def test_repeated_step_compaction_does_not_reappend_old_sdk_items():
    huge = HugeTool()
    comp = compactor()
    summaries = []

    async def summarize(messages):
        transcript = json.dumps(messages)
        if summaries:
            assert "checkpoint-1" in transcript
            assert transcript.count("LATE_EVIDENCE") == 1
        summaries.append(transcript)
        return f"checkpoint-{len(summaries)}: completed the preceding action; continue."

    comp.summarize_conversation_segment = summarize

    def script(turn, tools, input):
        if turn > 1:
            assert f"checkpoint-{turn - 1}" in json.dumps(input)
            assert "LATE_EVIDENCE" not in json.dumps(input)
        return [call("huge", f"call-{turn}")] if turn < 3 else [final_message()]

    agent = CompactingAgent(name="worker", model=ScriptedModel(script), tools=convert_tools([huge]))
    controller = SDKContextCompaction(agent, comp, [{"role": "user", "content": "two actions"}], [huge])
    result = Runner.run_streamed(agent, input="two actions", run_config=RunConfig(tracing_disabled=True))
    controller.bind(result)
    events = [event async for event in _stream_sdk_events(result, [huge])]
    assert huge.calls == comp.compression_count == 2
    assert len(result.raw_responses) == 3
    assert len([event for event in events if event["code"] == 5]) == 2
    assert controller.snapshot()[-1]["content"] == "done"


@pytest.mark.asyncio
async def test_failed_step_compaction_keeps_sdk_items_and_does_not_replay_tools():
    huge = HugeTool()
    comp = compactor()
    comp.summarize_conversation_segment = AsyncMock(side_effect=CompressionUnavailable("offline"))

    def script(turn, tools, input):
        if turn == 1:
            return [call("huge", "once")]
        assert "LATE_EVIDENCE" in json.dumps(input)
        return [final_message()]

    agent = CompactingAgent(name="worker", model=ScriptedModel(script), tools=convert_tools([huge]))
    controller = SDKContextCompaction(agent, comp, [], [huge])
    result = Runner.run_streamed(agent, input="one action", run_config=RunConfig(tracing_disabled=True))
    controller.bind(result)
    _ = [event async for event in _stream_sdk_events(result, [huge])]
    assert huge.calls == 1 and comp.compression_count == 0
    assert "LATE_EVIDENCE" in json.dumps(controller.snapshot())


@pytest.mark.asyncio
async def test_reloaded_guide_can_be_used_above_soft_watermark(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    manager = SkillManager(workspace)
    manager.save_user_skill(
        "alice", Skill(name="large-guide", description="Guide", instructions="EXACT_GUIDE " * 600)
    )
    skill = SkillTool(user_id="alice", skill_manager=manager, workspace=workspace)
    comp = HistoryCompressor(
        max_history_tokens=10000,
        compression_threshold=0.1,
        min_messages_to_keep=0,
        async_client=SimpleNamespace(),
    )
    comp.summarize_conversation_segment = AsyncMock(return_value="Reload large-guide to continue.")
    mounter = SkillToolMounter([skill], user_id="alice", session_id="thread", workspace=workspace)
    comp.compaction_callbacks.append(mounter.on_context_compacted)

    def script(turn, tools, input):
        if turn <= 2:
            return [call("skill", f"load-{turn}", {"action": "get", "name": "large-guide"})]
        assert "EXACT_GUIDE" in json.dumps(input)
        return [final_message("The guide remained available for execution.")]

    agent = CompactingAgent(name="worker", model=ScriptedModel(script), tools=convert_tools([skill]))
    controller = SDKContextCompaction(agent, comp, [{"role": "user", "content": "run"}], [skill])
    result = Runner.run_streamed(agent, input="run", run_config=RunConfig(tracing_disabled=True), max_turns=4)
    controller.bind(result)
    _ = [event async for event in _stream_sdk_events(result, [skill])]
    assert comp.compression_count == 1
    assert result.final_output == "The guide remained available for execution."


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "child"])
async def test_real_agent_entrypoints_persist_compacted_history(tmp_path, monkeypatch, kind):
    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(settings, "TOOLSEARCH_ALWAYS_LOADED", ("huge",))
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    huge = HugeTool()
    comp = compactor()
    comp.summarize_conversation_segment = AsyncMock(
        return_value="The tool ran and produced its result. Continue."
    )

    def script(turn, tools, input):
        if turn == 1:
            return [call("huge", "execute")]
        assert "<summary>" in json.dumps(input) and "LATE_EVIDENCE" not in json.dumps(input)
        return [final_message("Final result delivered.")]

    model = ScriptedModel(script)
    runner = SimpleNamespace(
        run_streamed=lambda *args, **kwargs: Runner.run_streamed(
            *args, **kwargs, run_config=RunConfig(tracing_disabled=True)
        )
    )
    monkeypatch.setattr(main, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    if kind == "main":
        parent = EvoAgent.__new__(EvoAgent)
        parent.user_id, parent.session_id = "alice", "thread"
        parent.workspace, parent.history, parent.tools = workspace, [], [huge]
        parent.system_prompt, parent.model, parent._compressor = "Test", model, comp
        parent._skill_tools = SkillToolMounter(
            parent.tools, user_id="alice", session_id="thread", workspace=workspace
        )
        monkeypatch.setattr(main, "Runner", runner)
        _ = [event async for event in parent._stream_agent_events("one action")]
        await parent._post_session()
        saved = workspace.history_load("thread")
    else:
        monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
        monkeypatch.setattr(sub, "MODEL", model)
        monkeypatch.setattr(sub, "Runner", runner)
        monkeypatch.setattr(sub, "HistoryCompressor", lambda **kwargs: comp)
        child = sub.SubAgentRunner(
            sub.AgentTypeConfig(name="worker"), user_id="alice", session_id="thread", workspace=workspace
        )
        monkeypatch.setattr(child, "_build_tools", lambda: [huge])
        async for event in child.run_stream("one action"):
            if event.get("content") == "stream_end":
                saved = workspace.history_load(child.session_id)
    assert saved["conversation"][0]["content"].startswith("<summary>")
    assert saved["conversation"][-1]["content"] == "Final result delivered."
    assert "LATE_EVIDENCE" not in json.dumps(saved)
