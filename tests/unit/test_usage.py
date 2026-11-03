"""Measured usage through real SDK loops, delegation, compaction and scope cleanup."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, RunConfig, Runner
from openai.types.responses import ResponseCompletedEvent
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails, ResponseUsage

from configs import settings
from core.agent import EvoAgent
from core.plugins.internal import SubAgentParams, SubAgentTool
from core.runtime.execution import SDKRunSession
from core.runtime.usage import capture_usage, record_sdk_usage
from core.sub_agent import AgentTypeConfig
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from utils.history_compressor import HistoryCompressor
from utils.storage_backend.localfs import LocalFSBackend
from utils.usage import current_usage, record_provider_usage, usage_scope
from utils.workspace import Workspace


class MeasuredModel(ScriptedModel):
    async def stream_response(self, *args, **kwargs):
        async for event in super().stream_response(*args, **kwargs):
            if isinstance(event, ResponseCompletedEvent):
                event.response.model = "actual-model"
                event.response.usage = ResponseUsage(
                    input_tokens=11,
                    output_tokens=7,
                    total_tokens=18,
                    input_tokens_details=InputTokensDetails(cached_tokens=3),
                    output_tokens_details=OutputTokensDetails(reasoning_tokens=2),
                )
            yield event


@pytest.mark.asyncio
async def test_sdk_counts_measured_usage_once_with_actual_model_and_details():
    with usage_scope() as collector:
        agent = Agent(name="probe", model=MeasuredModel(lambda *args: [final_message()]))
        session = SDKRunSession(agent, HistoryCompressor(compression_level="none"), [], [])
        session.start(
            lambda *args, **kwargs: Runner.run_streamed(
                *args, **kwargs, run_config=RunConfig(tracing_disabled=True)
            ),
            agent,
            input="test",
            max_turns=2,
        )
        async for _ in session.stream():
            pass
        record_sdk_usage(
            collector,
            session.result,
            run_id=session.run_id,
            model=session.model,
            category="main",
            response_models=session.response_models,
        )
        result = collector.snapshot()
    assert current_usage() is None
    assert result["usage"] == {
        "requests": 1,
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cached_input_tokens": 3,
        "reasoning_tokens": 2,
    }
    assert result["usage_by_model"]["actual-model"]["total_tokens"] == 18


@pytest.mark.asyncio
async def test_main_metrics_separate_context_size_from_provider_usage(tmp_path, monkeypatch):
    import core.agent as main

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main, "ENABLE_METRICS", True)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    agent = EvoAgent(
        "alice",
        "usage",
        plugins=[],
        workspace=workspace,
        system_prompt="Offline",
        model=MeasuredModel(lambda *args: [final_message("A substantive report")]),
    )
    agent.skill_matcher.match = AsyncMock(return_value=SimpleNamespace(skill=None))
    events = [event async for event in agent.run_stream("test")]
    metrics = json.loads(next(event["content"] for event in events if event["code"] == 6))
    assert metrics["usage"]["total_tokens"] == 18 and metrics["context_tokens"] > 0
    assert metrics["usage_by_category"]["main"]["requests"] == 1
    assert current_usage() is None


@pytest.mark.asyncio
async def test_child_inherits_the_parent_collector_explicitly(tmp_path, monkeypatch):
    import core.sub_agent as sub

    seen = []

    class Child:
        def __init__(self, **kwargs):
            self.inherited_usage = kwargs["usage_collector"]
            self.config = SimpleNamespace(name="worker")
            self.session_id = kwargs["session_id"]

        @capture_usage
        async def run_stream(self, task, context):
            seen.append(current_usage())
            record_provider_usage(
                SimpleNamespace(model="child-model", usage={"prompt_tokens": 4, "completion_tokens": 2}),
                category="child",
            )
            yield {"code": 2, "content": "done"}

    monkeypatch.setattr(sub, "SubAgentRunner", Child)
    with usage_scope() as parent:
        tool = SubAgentTool("alice", "main", workspace=Workspace("alice", LocalFSBackend(tmp_path)))
        await tool.delegate(SubAgentParams(task="test", agent_type="worker"))
        assert seen == [parent] and parent.snapshot()["usage"]["total_tokens"] == 6


@pytest.mark.asyncio
async def test_independent_turn_scopes_do_not_mix_and_reset_on_cancellation():
    entered = asyncio.Event()

    class Turn:
        @capture_usage
        async def run(self, block=False):
            record_provider_usage(SimpleNamespace(model="model", usage={"input_tokens": 5}), category="main")
            if block:
                entered.set()
                await asyncio.Event().wait()
            yield current_usage()

    first, second = Turn(), Turn()
    with usage_scope() as outer:
        scopes = [value async for value in first.run()] + [value async for value in second.run()]
        assert scopes[0] is not scopes[1] and all(scope is not outer for scope in scopes)
        assert outer.snapshot()["usage"]["requests"] == 0

        async def consume():
            async for _ in Turn().run(block=True):
                pass

        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert current_usage() is outer
    assert current_usage() is None


@pytest.mark.asyncio
async def test_summary_calls_record_usage_even_when_output_is_rejected():
    response = SimpleNamespace(
        model="summary-model",
        usage={"prompt_tokens": 20, "completion_tokens": 3},
        choices=[
            SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="<summary>partial"))
        ],
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )
    compressor = HistoryCompressor(async_client=client)
    with usage_scope() as collector:
        with pytest.raises(Exception, match="finish normally"):
            await compressor._summarize_transcript("history")
        assert collector.snapshot()["usage_by_category"]["compression"]["total_tokens"] == 23


def test_missing_provider_usage_does_not_invent_tokens():
    with usage_scope() as collector:
        record_provider_usage(SimpleNamespace(model="missing"), category="main")
        assert collector.snapshot()["usage"]["requests"] == 0
        assert not collector.snapshot()["usage_by_model"]


@pytest.mark.asyncio
async def test_real_parent_and_child_sdk_usage_is_combined_without_double_counting(tmp_path, monkeypatch):
    from openai.types.responses import ResponseFunctionToolCall

    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main, "ENABLE_METRICS", True)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(sub, "load_agent_config", lambda name: AgentTypeConfig(name=name))
    child_model = MeasuredModel(lambda *args: [final_message("A substantive child report")])
    monkeypatch.setattr(
        sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": child_model}))
    )

    def script(turn, tools, input):
        if turn == 1:
            return [
                ResponseFunctionToolCall(
                    type="function_call",
                    call_id="child",
                    name="sub_agent",
                    arguments=json.dumps({"agent_type": "worker", "task": "report", "task_id": "a"}),
                )
            ]
        return [final_message()]

    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    agent = EvoAgent(
        "alice",
        "usage",
        plugins=[],
        workspace=workspace,
        system_prompt="Offline",
        model=MeasuredModel(script),
    )
    agent.skill_matcher.match = AsyncMock(return_value=SimpleNamespace(skill=None))
    events = [event async for event in agent.run_stream("report")]
    metrics = json.loads(next(event["content"] for event in events if event["code"] == 6))
    assert metrics["usage"]["requests"] == 3 and metrics["usage"]["total_tokens"] == 54
    assert metrics["usage_by_category"]["main"]["total_tokens"] == 36
    assert metrics["usage_by_category"]["child"]["total_tokens"] == 18
    assert child_model.turn == 1
