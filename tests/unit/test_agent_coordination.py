"""Offline acceptance for task continuation, concurrent delivery and receipts."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agents import Agent, Model, RunConfig, Runner
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)
from pydantic import ValidationError

from core.agent import (
    EvoAgent,
    _attach_delivery_events,
    _history_to_sdk_input,
    _sdk_result_to_history,
    _stream_sdk_events,
)
from core.agent_compat import convert_tools
from core.delegation import automatic_task_id, task_session_base
from core.plugins.internal import SubAgentParams, SubAgentTool
from core.skills import Skill
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", LocalFSBackend(tmp_path))


class StoredChild:
    """A deterministic worker that exercises real local history persistence."""

    gate = None

    def __init__(self, agent_type="worker", **kwargs):
        self.user_id = kwargs["user_id"]
        self.session_id = "sa_worker_" + kwargs["session_id"]
        self.workspace = kwargs["workspace"]
        self.config = SimpleNamespace(name=agent_type)

    @classmethod
    def from_skill(cls, skill, **kwargs):
        return cls(agent_type=skill.sub_agent_name or skill.name, **kwargs)

    async def run_stream(self, task, context):
        old = self.workspace.history_load(self.session_id) or {"conversation": []}
        history = old["conversation"]
        yield {"code": 3, "content": "stream_start"}
        yield {"code": 2, "content": f"{task}: seen {len(history)} turns; "}
        if self.gate:
            await self.gate.wait()
        if task == "ask":
            yield {
                "code": 10,
                "content": json.dumps({"type": "ask_user", "question": "Which format?", "options": []}),
            }
        elif task == "fail":
            raise RuntimeError("worker unavailable")
        else:
            yield {
                "code": 5,
                "content": json.dumps(
                    {
                        "name": "report",
                        "status": "success",
                        "is_error": False,
                        "artifacts": [{"url": "https://example.com/final.pdf", "name": "final.pdf"}],
                    }
                ),
            }
            yield {"code": 2, "content": "final " + "complete body " * 600 + " https://example.com/final.pdf"}
        history.append({"role": "assistant", "content": task})
        self.workspace.history_save(self.session_id, {"conversation": history})
        yield {"code": 6, "content": "child metrics"}
        yield {"code": 3, "content": "stream_end"}


@pytest.fixture
def child(monkeypatch):
    import core.sub_agent as module

    monkeypatch.setattr(module, "SubAgentRunner", StoredChild)
    monkeypatch.setattr(StoredChild, "gate", None)
    return StoredChild


@pytest.mark.asyncio
async def test_task_history_continues_across_tool_instances_and_isolates_other_tasks(workspace, child):
    events = []

    async def emit(frame):
        events.append(frame)

    async def call(task_id, parent="main"):
        tool = SubAgentTool(user_id="alice", session_id=parent, workspace=workspace)
        tool.event_sink = emit
        return json.loads(await tool(SubAgentParams(task="report", agent_type="worker", task_id=task_id)))

    first = await call("report-a")
    second = await call("report-a")
    third = await call("report-b")
    other_parent = await call("report-a", parent="other")
    assert first["session_id"] == second["session_id"]
    assert len({first["session_id"], third["session_id"], other_parent["session_id"]}) == 3
    assert "seen 1 turns" in second["summary"]
    assert "seen 0 turns" in third["summary"]
    assert first["artifact_refs"] == ["https://example.com/final.pdf"]
    assert all(event.get("task_id") and event.get("delivery_owner") for event in events)
    assert not any(event["code"] in (3, 6) for event in events)
    assert sum(len(event["content"]) for event in events if event["code"] == 2) > 24000


@pytest.mark.asyncio
async def test_missing_ids_create_new_tasks_and_invalid_ids_are_rejected(workspace, child):
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)
    receipts = [await tool.delegate(SubAgentParams(task="report", agent_type="worker")) for _ in range(2)]
    assert receipts[0]["task_id"] != receipts[1]["task_id"]
    assert receipts[0]["session_id"] != receipts[1]["session_id"]
    for value in ("../other", "", "x" * 65, "a/b"):
        with pytest.raises(ValidationError):
            SubAgentParams(task="report", agent_type="worker", task_id=value)
    assert task_session_base("x" * 100 + "a", "task") != task_session_base("x" * 100 + "b", "task")


@pytest.mark.asyncio
@pytest.mark.parametrize("task,status", [("ask", "waiting_user"), ("fail", "failed")])
async def test_receipt_distinguishes_questions_and_partial_failures(workspace, child, task, status):
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)
    receipt = await tool.delegate(SubAgentParams(task=task, agent_type="worker", task_id="a"))
    assert receipt["status"] == status
    if task == "ask":
        assert receipt["delivery_complete"]
        assert receipt["resume_task_id"] == "a"
        assert receipt["pending_questions"][0]["question"] == "Which format?"
    else:
        assert not receipt["delivery_complete"]
        assert receipt["failures"] == ["worker unavailable"]
        assert 2 in receipt["delivered_codes"]


class ScriptedModel(Model):
    def __init__(self, script):
        self.script = script
        self.turn = 0

    def response(self, tools, input):
        self.turn += 1
        return ModelResponse(
            output=self.script(self.turn, tools, input), usage=Usage(), response_id=f"r-{self.turn}"
        )

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs
    ):
        return self.response(tools, input)

    async def stream_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs
    ):
        response = self.response(tools, input)
        for item in response.output:
            if isinstance(item, ResponseOutputMessage):
                yield ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    sequence_number=0,
                    item_id=item.id,
                    output_index=0,
                    content_index=0,
                    delta=item.content[0].text,
                    logprobs=[],
                )
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=1,
            response=Response(
                id=response.response_id,
                created_at=0,
                model="offline",
                object="response",
                output=response.output,
                parallel_tool_calls=True,
                tool_choice="auto",
                tools=[],
            ),
        )


def final_message(text="done"):
    return ResponseOutputMessage(
        type="message",
        id="final",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


@pytest.mark.asyncio
async def test_real_sdk_streams_parallel_children_before_completion_and_keeps_receipts(workspace, child):
    child.gate = asyncio.Event()
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)

    def script(turn, tools, input):
        if turn == 1:
            return [
                ResponseFunctionToolCall(
                    type="function_call",
                    call_id=f"call-{name}",
                    name="sub_agent",
                    arguments=json.dumps({"task": name, "agent_type": "worker", "task_id": name}),
                )
                for name in ("a", "b")
            ]
        receipts = [
            json.loads(item["output"]) for item in input if item.get("type") == "function_call_output"
        ]
        assert {r["task_id"] for r in receipts} == {"a", "b"}
        assert all(r["status"] == "delivered" for r in receipts)
        assert all(len(r["summary"]) < 1600 for r in receipts)
        return [final_message()]

    agent = Agent(name="parent", model=ScriptedModel(script), tools=convert_tools([tool]))
    result = Runner.run_streamed(agent, input="two reports", run_config=RunConfig(tracing_disabled=True))
    events = []
    async with asyncio.timeout(3):
        stream = _stream_sdk_events(result, [tool])
        try:
            seen = set()
            async for event in stream:
                events.append(event)
                if event.get("task_id") and event["code"] == 2:
                    seen.add(event["task_id"])
                    if seen == {"a", "b"}:
                        assert not result.is_complete
                        child.gate.set()
        finally:
            await stream.aclose()
    assert {event["task_id"] for event in events if event.get("task_id")} == {"a", "b"}
    assert sum(len(e["content"]) for e in events if e["code"] == 2) > 14000
    history = _sdk_result_to_history([], result)
    _attach_delivery_events(history, [tool])
    receipts = [msg for msg in history if msg.get("name") == "sub_agent"]
    assert len(receipts) == 2 and all(msg["delivery_events"] for msg in receipts)
    restored = _history_to_sdk_input(history)
    assert "delivery_events" not in json.dumps(restored)
    assert sum("complete body" in item.get("output", "") for item in restored) == 2


@pytest.mark.asyncio
async def test_same_thread_calls_start_without_waiting_for_an_execution_lock(workspace, child):
    child.gate = asyncio.Event()
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)
    started = {name: asyncio.Event() for name in ("first", "second")}

    async def emit(frame):
        if frame.get("code") == 2:
            for name, event in started.items():
                if frame.get("content", "").startswith(f"{name}: seen"):
                    event.set()

    calls = [
        asyncio.create_task(
            tool.delegate(SubAgentParams(task=name, agent_type="worker", task_id="same"), emit=emit)
        )
        for name in started
    ]
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(*(event.wait() for event in started.values()))
            assert all(not call.done() for call in calls)
            child.gate.set()
            receipts = await asyncio.gather(*calls)
    finally:
        child.gate.set()
        for call in calls:
            if not call.done():
                call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)

    assert receipts[0]["session_id"] == receipts[1]["session_id"]
    assert all(receipt["status"] == "delivered" for receipt in receipts)
    assert receipts[0]["summary"].startswith("first: seen")
    assert receipts[1]["summary"].startswith("second: seen")


@pytest.mark.asyncio
async def test_automatic_handoff_shares_identity_delivery_and_persists_before_end(workspace, child):
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)
    skill = Skill(
        name="report", description="Report workflow", requires_sub_agent=True, sub_agent_name="worker"
    )
    parent = EvoAgent.__new__(EvoAgent)
    parent.task_id = "same"
    parent._match_result = SimpleNamespace(skill=skill)
    parent.tools, parent.history = [tool], []
    parent._post_session = AsyncMock()
    async for event in parent._handle_sub_agent_handoff("report"):
        if event.get("content") == "stream_start":
            assert parent.history[-1] == {"role": "user", "content": "report"}
        if event.get("content") == "stream_end":
            parent._post_session.assert_awaited_once()
    saved = parent.history[-1]
    receipt = json.loads(saved["content"])
    explicit = await tool.delegate(SubAgentParams(task="revise", agent_type="worker", task_id="same"))
    assert receipt["session_id"] == explicit["session_id"]
    assert "seen 1 turns" in explicit["summary"]
    assert saved["delivery_events"] and len(saved["content"]) < 4000
    assert automatic_task_id("report") == automatic_task_id("report")
    assert automatic_task_id("report") != automatic_task_id("other")


@pytest.mark.asyncio
async def test_skill_backed_worker_can_resume_through_explicit_tool(workspace, monkeypatch):
    import core.sub_agent as module
    from core.skills.manager import SkillManager

    skill = Skill(
        name="custom-workflow",
        description="Custom workflow",
        requires_sub_agent=True,
        sub_agent_name="custom-worker-without-config",
    )
    SkillManager(workspace).save_user_skill("alice", skill)
    monkeypatch.setattr(module.SubAgentRunner, "run_stream", StoredChild.run_stream)
    monkeypatch.setattr(module.SubAgentRunner, "gate", None, raising=False)
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)
    params = SubAgentParams(task="report", agent_type=skill.sub_agent_name, task_id="custom-a")
    automatic = await tool.delegate(params, skill=skill)
    explicit = json.loads(await tool(params))
    assert automatic["session_id"] == explicit["session_id"]
    assert "seen 1 turns" in explicit["summary"]


@pytest.mark.asyncio
async def test_stream_close_cancels_sdk_and_restores_channel(workspace):
    tool = SubAgentTool(user_id="alice", workspace=workspace)
    closed = asyncio.Event()

    class Result:
        is_complete = False
        cancelled = False

        def cancel(self):
            self.cancelled = True

        async def stream_events(self):
            try:
                await tool.event_sink({"code": 2, "content": "first", "task_id": "a"})
                await asyncio.Event().wait()
                yield None
            finally:
                closed.set()

    result = Result()
    stream = _stream_sdk_events(result, [tool])
    assert (await anext(stream))["content"] == "first"
    await stream.aclose()
    assert result.cancelled and closed.is_set() and tool.event_sink is None


@pytest.mark.asyncio
async def test_real_sdk_cancellation_closes_a_running_child(workspace, child, monkeypatch):
    child.gate = asyncio.Event()
    closed = asyncio.Event()
    original = child.run_stream

    async def tracked(self, *args):
        try:
            async for event in original(self, *args):
                yield event
        finally:
            closed.set()

    monkeypatch.setattr(child, "run_stream", tracked)
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)

    def script(turn, tools, input):
        return [
            ResponseFunctionToolCall(
                type="function_call",
                call_id="child",
                name="sub_agent",
                arguments='{"task":"work","agent_type":"worker","task_id":"a"}',
            )
        ]

    result = Runner.run_streamed(
        Agent(name="parent", model=ScriptedModel(script), tools=convert_tools([tool])),
        input="work",
        run_config=RunConfig(tracing_disabled=True),
    )
    stream = _stream_sdk_events(result, [tool])
    async with asyncio.timeout(3):
        async for event in stream:
            if event.get("task_id") == "a" and event["code"] == 2:
                break
        await stream.aclose()
        await closed.wait()
    assert result.is_complete and tool.event_sink is None


@pytest.mark.asyncio
async def test_unknown_worker_has_a_failed_receipt_and_automatic_handoff_reports_it(workspace, monkeypatch):
    import core.sub_agent as module

    class MissingWorker:
        def __init__(self, **kwargs):
            raise ValueError("Worker unavailable")

        @classmethod
        def from_skill(cls, skill, **kwargs):
            return cls(**kwargs)

    monkeypatch.setattr(module, "SubAgentRunner", MissingWorker)
    tool = SubAgentTool(user_id="alice", session_id="main", workspace=workspace)
    receipt = json.loads(await tool(SubAgentParams(task="work", agent_type="missing", task_id="a")))
    assert receipt["type"] == "sub_agent_receipt" and receipt["task_id"] == "a"
    assert receipt["status"] == "failed" and not receipt["delivery_complete"]
    parent = EvoAgent.__new__(EvoAgent)
    parent.task_id = "a"
    parent._match_result = SimpleNamespace(
        skill=Skill(name="work", description="Work", requires_sub_agent=True)
    )
    parent.tools, parent.history = [tool], []
    parent._post_session = AsyncMock()
    events = [event async for event in parent._handle_sub_agent_handoff("work")]
    assert any(event["code"] == 2 and "failed" in event["content"] for event in events)
    assert parent.history[-1]["delivery_events"]


def test_frontend_keeps_parallel_json_text_and_task_bubbles_separate():
    if not shutil.which("node"):
        pytest.skip("Node.js required for task stream checks")
    html = Path("app/static/index.html").read_text()
    functions = (
        "function accumulateTaskText"
        + html.split("function accumulateTaskText", 1)[1].split("function replayDelegatedDelivery", 1)[0]
    )
    flow_functions = (
        "const _taskFlowStates ="
        + html.split("const _taskFlowStates =", 1)[1].split("function renderTodoPanel", 1)[0]
    )
    script = r"""
const assert = require('assert');
let messages = [];
function createBotMsg() {
  const bubble = {textContent: 'Thinking...'};
  const msg = {dataset: {}, querySelector: () => bubble};
  messages.push(msg);
  return msg;
}
const root = {textContent: 'Thinking...'};
const stream = createTaskStream(root);
const texts = new Map();
const a = {task_id:'a', source_agent:'worker', content:'{"a":'};
const b = {task_id:'b', source_agent:'worker', content:'{"b":'};
accumulateTaskText(texts, a);
accumulateTaskText(texts, b);
accumulateTaskText(texts, {...a, content:'1}'});
const joined = accumulateTaskText(texts, {...b, content:'2}'});
assert.deepStrictEqual(joined.split('\n\n').map(JSON.parse), [{a:1}, {b:2}]);
assert.strictEqual(stream.bubbleFor(a), stream.bubbleFor({...a, content:'next'}));
assert.notStrictEqual(stream.bubbleFor(a), stream.bubbleFor(b));
assert.strictEqual(stream.bubbleFor({content:'parent'}), root);
assert.strictEqual(messages.length, 2);
const ownerA = {}, ownerB = {};
flowStateFor(ownerA).pending.push({label:'Search A'});
flowStateFor(ownerA).steps.step1 = ['A'];
assert.deepStrictEqual(flowStateFor(ownerB), {pending:[], steps:{}});
assert.strictEqual(flowStateFor(ownerA).pending[0].label, 'Search A');
"""
    subprocess.run(
        ["node", "-e", functions + flow_functions + script], check=True, capture_output=True, text=True
    )


@pytest.mark.asyncio
async def test_chat_api_passes_task_identity_and_extracts_each_parallel_response(workspace, monkeypatch):
    from fastapi import FastAPI

    import app.api.routers.agent as router

    instances, extracted = [], []

    class OfflineAgent:
        def __init__(self, **kwargs):
            self.workspace = workspace
            instances.append(kwargs)

        async def run_stream(self, user_input):
            yield {"code": 3, "content": "stream_start"}
            for task_id, text in (("a", '{"a":'), ("b", '{"b":'), ("a", "1}"), ("b", "2}")):
                yield {"code": 2, "content": text, "task_id": task_id, "source_agent": "worker"}
            yield {"code": 3, "content": "stream_end"}

    def extract(text):
        extracted.append(json.loads(text))
        return None

    monkeypatch.setattr(router, "EvoAgent", OfflineAgent)
    monkeypatch.setattr(router, "_create_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(router, "INPUT_EXTRACTORS", {})
    monkeypatch.setattr(router, "OUTPUT_EXTRACTORS", {"test": extract})
    app = FastAPI()
    app.include_router(router.router)
    payload = {
        "userId": "alice",
        "sessionId": "main",
        "requestId": "request-1",
        "taskId": "report-a",
        "answerTo": {"question_id": "question-a", "agent_type": "worker", "task_id": "report-a"},
        "data": {"input": "two reports", "files": [{"name": "rules.txt", "content": "Original rules"}]},
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/chat", json=payload)
        invalid = await client.post("/chat", json=payload | {"taskId": "../other"})
    assert response.status_code == 200 and invalid.status_code == 422
    assert instances[0]["task_id"] == "report-a"
    assert instances[0]["answer_to"]["question_id"] == "question-a"
    assert instances[0]["original_input"] == "two reports"
    assert instances[0]["attachments"]["files"][0]["content"] == "Original rules"
    assert extracted == [{"a": 1}, {"b": 2}]
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert all(event["requestId"] == "request-1" for event in events)
    assert {event["task_id"] for event in events if event["code"] == 2} == {"a", "b"}
