"""Cancellation persistence through real SDK loops and the HTTP stream boundary."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from configs import settings
from core.agent import EvoAgent
from core.agent_compat import BaseTool
from core.runtime.history import _history_to_sdk_input
from core.runtime.interruption import INTERRUPTION_NOTE, TurnPersistence
from core.skills import Skill
from core.sub_agent import AgentTypeConfig, SubAgentRunner
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


class Params(BaseModel):
    mode: str = "slow"
    text: str = ""


class PausedTool(BaseTool):
    name = "paused"
    description = "Offline tool with observable execution"
    param_anno = Params

    def __init__(self):
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stopped = asyncio.Event()
        self.calls = []

    async def __call__(self, params):
        self.calls.append(params.mode)
        if params.mode == "fast":
            self.finished.set()
            return "EXACT_COMPLETED_RESULT"
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", LocalFSBackend(tmp_path))


@pytest.fixture
def agents(workspace, monkeypatch):
    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(sub, "OPENAI_API_KEY", "offline-test")

    def make(model, tools=(), kind="main"):
        if kind == "main":
            agent = EvoAgent(
                user_id="alice",
                session_id="thread",
                plugins=[],
                system_prompt="Offline",
                model=model,
                workspace=workspace,
            )
            agent.skill_matcher.match = AsyncMock(return_value=SimpleNamespace(skill=None))
            agent.tools.extend(tools)
        else:
            agent = SubAgentRunner(
                AgentTypeConfig(name="worker"), user_id="alice", session_id="thread", workspace=workspace
            )
            monkeypatch.setattr(agent, "_build_tools", lambda: list(tools))
            monkeypatch.setattr(agent, "_check_completion", AsyncMock(return_value=True))
            monkeypatch.setattr(
                sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": model}))
            )
        return agent

    return make


def model_for_calls(parallel=False):
    from openai.types.responses import ResponseFunctionToolCall

    def script(turn, tools, input):
        modes = ["fast", "slow"] if parallel else ["slow"]
        return [
            ResponseFunctionToolCall(
                type="function_call",
                call_id=mode,
                name="paused",
                arguments=json.dumps({"mode": mode, "text": "FULL_ARGUMENT " * 200}),
            )
            for mode in modes
        ]

    return ScriptedModel(script)


async def consume(source):
    async for _ in source:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "child"])
async def test_cancel_keeps_completed_parallel_result_and_marks_pending_call_unknown(agents, workspace, kind):
    tool = PausedTool()
    agent = agents(model_for_calls(parallel=True), [tool], kind)
    task = asyncio.create_task(consume(agent.run_stream("continue")))
    await asyncio.wait_for(tool.started.wait(), 2)
    await tool.finished.wait()
    # Let the SDK's per-tool completion hook run while the sibling remains blocked.
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = workspace.history_load(agent.session_id)
    assert saved["interrupted"] and saved["interruption_reason"] == "cancelled"
    history = saved["conversation"]
    outputs = {message["tool_call_id"]: message for message in history if message.get("role") == "tool"}
    assert outputs["fast"]["content"] == "EXACT_COMPLETED_RESULT"
    assert json.loads(outputs["slow"]["content"])["outcome"] == "unknown"
    assert outputs["slow"]["execution_status"] == "interrupted"
    assert tool.calls.count("slow") == tool.calls.count("fast") == 1
    calls = [call for message in history for call in message.get("tool_calls", [])]
    assert len(calls) == 2 and all("FULL_ARGUMENT " * 200 in call["function"]["arguments"] for call in calls)
    sdk_input = _history_to_sdk_input(history)
    assert {item["call_id"] for item in sdk_input if item.get("type") == "function_call"} == {"fast", "slow"}
    assert history[-1]["content"] == INTERRUPTION_NOTE
    assert (
        sum(message.get("role") == "user" and message.get("content") == "continue" for message in history)
        == 1
    )
    await asyncio.wait_for(tool.stopped.wait(), 1)


class PartialModel(ScriptedModel):
    async def stream_response(self, *args, **kwargs):
        yield ResponseTextDeltaEvent(
            type="response.output_text.delta",
            sequence_number=0,
            item_id="partial",
            output_index=0,
            content_index=0,
            delta='{"unfinished": "partial text',
            logprobs=[],
        )
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "child"])
async def test_explicit_close_preserves_partial_reply_without_inventing_a_complete_card(
    agents, workspace, kind
):
    agent = agents(PartialModel(lambda *args: []), kind=kind)
    stream = agent.run_stream("draft")
    async for event in stream:
        if event.get("code") == 2:
            break
    await stream.aclose()
    saved = workspace.history_load(agent.session_id)
    partial = [message for message in saved["conversation"] if message.get("partial")]
    assert len(partial) == 1
    assert partial[0]["content"].endswith('{"unfinished": "partial text')
    assert "Incomplete assistant response" in partial[0]["content"]
    assert saved["interrupted"] and saved["interruption_reason"] == "closed"


@pytest.mark.asyncio
async def test_cancel_during_unread_load_does_not_replace_existing_history(agents, workspace, monkeypatch):
    original = {"conversation": [{"role": "user", "content": "original"}]}
    workspace.history_save("thread", original)
    agent = agents(model_for_calls())
    started = asyncio.Event()

    async def load():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent, "load_session", load)
    task = asyncio.create_task(consume(agent.run_stream("new request")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert workspace.history_load("thread") == original


@pytest.mark.asyncio
async def test_close_before_sdk_start_keeps_repeated_user_input_once_per_turn(agents, workspace):
    workspace.history_save("thread", {"conversation": [{"role": "user", "content": "continue"}]})
    agent = agents(model_for_calls())
    stream = agent.run_stream("continue")
    assert (await anext(stream))["content"] == "stream_start"
    await stream.aclose()
    saved = workspace.history_load("thread")
    assert [message["content"] for message in saved["conversation"] if message["role"] == "user"] == [
        "continue",
        "continue",
    ]


@pytest.mark.asyncio
async def test_storage_write_survives_repeated_cancellation_and_is_not_duplicated():
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def writer(record):
        started.set()
        await release.wait()
        writes.append(record)

    persistence = TurnPersistence(None, "alice", "thread", writer=writer, timeout=1)
    task = asyncio.create_task(persistence.save([{"role": "user", "content": "work"}]))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert persistence.saved and len(writes) == 1
    await persistence.save_interrupted("cancelled")
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_interrupted_save_has_a_deadline_and_does_not_claim_success():
    async def writer(record):
        await asyncio.Event().wait()

    persistence = TurnPersistence(None, "alice", "thread", writer=writer, timeout=0.01)
    persistence.loaded(lambda: [])
    with pytest.raises(TimeoutError):
        await persistence.save_interrupted("closed")
    assert not persistence.saved and persistence.write_attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "child"])
async def test_closing_after_normal_completion_does_not_append_interruption(agents, workspace, kind):
    agent = agents(ScriptedModel(lambda *args: [final_message("A complete response.")]), kind=kind)
    stream = agent.run_stream("work")
    async for event in stream:
        if event.get("content") == "stream_end":
            break
    await stream.aclose()
    saved = workspace.history_load(agent.session_id)
    assert not saved.get("interrupted")
    assert saved["conversation"][-1]["content"] == "A complete response."
    assert sum(message.get("content") == "A complete response." for message in saved["conversation"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_interrupted_delegation_preserves_child_thread_and_parent_receipt(
    agents, workspace, monkeypatch, automatic
):
    import core.sub_agent as sub
    from utils.ws_call import ws_call

    tool = PausedTool()
    child_saved = asyncio.Event()
    children = []

    class Worker(SubAgentRunner):
        def __init__(self, agent_type, **kwargs):
            super().__init__(AgentTypeConfig(name="worker"), **kwargs)
            children.append(self)

            async def writer(record):
                await ws_call(workspace, "history_save", self.session_id, record)
                child_saved.set()

            self._persistence.writer = writer

        def _build_tools(self):
            return [tool]

        async def _check_completion(self, text):
            return False

    monkeypatch.setattr(sub, "MODEL", model_for_calls())
    monkeypatch.setattr(sub, "SubAgentRunner", Worker)

    def delegate_script(turn, tools, input):
        from openai.types.responses import ResponseFunctionToolCall

        return [
            ResponseFunctionToolCall(
                type="function_call",
                call_id="delegate",
                name="sub_agent",
                arguments='{"agent_type":"worker","task":"work"}',
            )
        ]

    parent = agents(ScriptedModel(delegate_script))
    if automatic:
        skill = Skill(name="handoff", description="Handoff", requires_sub_agent=True, sub_agent_name="worker")
        parent.skill_matcher.match = AsyncMock(
            return_value=SimpleNamespace(skill=skill, match_type="trigger", confidence=1.0)
        )
    task = asyncio.create_task(consume(parent.run_stream("work")))
    await asyncio.wait_for(tool.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(child_saved.wait(), 2)
    assert len(children) == 1
    child_record = workspace.history_load(children[0].session_id)
    assert child_record["interrupted"]
    parent_record = workspace.history_load(parent.session_id)
    receipts = [message for message in parent_record["conversation"] if message.get("name") == "sub_agent"]
    assert len(receipts) == 1
    receipt = json.loads(receipts[0]["content"])
    thread = receipt if automatic else receipt["delegations"][0]
    assert thread["task_id"] and thread["resume_task_id"] == thread["task_id"]
    assert thread["status"] == "interrupted" and not thread["delivery_complete"]


@pytest.mark.asyncio
async def test_cancel_after_user_append_during_sdk_setup_does_not_duplicate_user(
    agents, workspace, monkeypatch
):
    agent = agents(model_for_calls())
    started = asyncio.Event()

    async def setup():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent, "_create_sdk_agent", setup)
    task = asyncio.create_task(consume(agent.run_stream("once")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = workspace.history_load(agent.session_id)
    assert (
        sum(
            message.get("role") == "user" and message.get("content") == "once"
            for message in saved["conversation"]
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "child"])
async def test_http_disconnect_closes_agent_and_persists_under_anyio_cancellation(
    agents, workspace, monkeypatch, kind
):
    import app.api.routers.agent as main_router
    import app.api.routers.sub_agent as sub_router

    tool = PausedTool()
    agent = agents(model_for_calls(), [tool], kind)
    monkeypatch.setattr(main_router, "_create_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(main_router, "EvoAgent", lambda **kwargs: agent)
    monkeypatch.setattr(sub_router, "SubAgentRunner", lambda **kwargs: agent)
    monkeypatch.setattr(main_router, "INPUT_EXTRACTORS", {})
    monkeypatch.setattr(main_router, "OUTPUT_EXTRACTORS", {})
    app = FastAPI()
    app.include_router(main_router.router)
    app.include_router(sub_router.router, prefix="/child")
    path = "/chat" if kind == "main" else "/child/worker/chat"
    body = json.dumps({"userId": "alice", "sessionId": "thread", "data": {"input": "work"}}).encode()
    disconnected = asyncio.Event()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(tool.started.wait(), 2)
    disconnected.set()
    await asyncio.wait_for(task, 3)
    saved = workspace.history_load(agent.session_id)
    assert saved["interrupted"] and saved["conversation"][-1]["content"] == INTERRUPTION_NOTE


def test_shared_runtime_modules_do_not_import_agent_implementations():
    script = "import sys; import core.runtime.history, core.runtime.events, core.runtime.interruption, core.context_compaction; assert 'core.agent' not in sys.modules; assert 'core.sub_agent' not in sys.modules"
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
