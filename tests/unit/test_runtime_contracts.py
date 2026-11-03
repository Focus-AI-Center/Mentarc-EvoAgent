"""Question boundaries, deterministic delegation and explicit tool outcomes."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, RunConfig, Runner
from agents.run_context import RunContextWrapper
from openai.types.responses import ResponseFunctionToolCall
from pydantic import BaseModel

from configs import settings
from core.agent import EvoAgent
from core.agent_compat import BaseTool, ToolResult, convert_tool, convert_tools
from core.delegation import run_delegation, task_session_base
from core.plugins.ask_user import AskUserQuestionTool
from core.plugins.internal import SubAgentParams, SubAgentTool
from core.runtime.delegation_context import delegation_snapshot, render_delegation_context
from core.runtime.events import _stream_sdk_events
from core.runtime.questions import pending_questions, question_tool_behavior, resolve_answer, user_message
from core.sub_agent import AgentTypeConfig, SubAgentRunner
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from utils.history_compressor import HistoryCompressor
from utils.storage_backend.localfs import LocalFSBackend
from utils.tool_results import tool_observation
from utils.workspace import Workspace


class Params(BaseModel):
    value: str = ""


class BusinessTool(BaseTool):
    name = "business"
    description = "Offline business operation"
    param_anno = Params

    def __init__(self):
        self.calls = 0

    async def __call__(self, params):
        self.calls += 1
        return json.dumps({"success": True, "value": params.value})


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", LocalFSBackend(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "child"])
async def test_question_ends_its_sdk_run_after_the_parallel_batch(workspace, monkeypatch, kind):
    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    business = BusinessTool()

    def script(turn, tools, input):
        assert turn == 1, "The model must not be called again before the user answers"
        return [
            ResponseFunctionToolCall(
                type="function_call",
                call_id="ask",
                name="ask_user_question",
                arguments=json.dumps({"question": "Which format?", "options": [{"label": "PDF"}]}),
            ),
            ResponseFunctionToolCall(
                type="function_call", call_id="work", name="business", arguments='{"value":"independent"}'
            ),
        ]

    model = ScriptedModel(script)
    if kind == "main":
        agent = EvoAgent(
            "alice", "question", plugins=[], workspace=workspace, model=model, system_prompt="Offline"
        )
        agent.skill_matcher.match = AsyncMock(return_value=SimpleNamespace(skill=None))
        agent.tools.append(business)
    else:
        agent = SubAgentRunner(AgentTypeConfig(name="worker"), "alice", "question", workspace=workspace)
        monkeypatch.setattr(agent, "_build_tools", lambda: [AskUserQuestionTool(), business])
        monkeypatch.setattr(sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": model})))
    events = [event async for event in agent.run_stream("Ask before continuing")]
    question = json.loads(next(event["content"] for event in events if event["code"] == 10))
    assert question["question_id"] and business.calls == 1 and model.turn == 1
    saved = workspace.history_load(agent.session_id)
    assert not saved.get("interrupted")
    assert question["question_id"] in pending_questions(saved["conversation"])
    assert any(message.get("name") == "business" for message in saved["conversation"])


def test_answer_routes_require_a_pending_saved_question():
    question = {"question_id": "question-a", "question": "Format?", "options": []}
    receipt = {
        "type": "sub_agent_receipt",
        "status": "waiting_user",
        "owner": "worker",
        "task_id": "report-a",
        "pending_questions": [question],
    }
    history = [{"role": "tool", "content": json.dumps(receipt)}]
    answer = resolve_answer(history, {"question_id": "question-a"})
    assert answer["agent_type"] == "worker" and answer["task_id"] == "report-a"
    with pytest.raises(ValueError, match="does not match"):
        resolve_answer(history, {"question_id": "question-a", "task_id": "other"})
    history.append(user_message("PDF", answer))
    with pytest.raises(ValueError, match="no longer pending"):
        resolve_answer(history, {"question_id": "question-a"})
    summary = {"role": "user", "pending_questions": [], "delegation_receipts": [receipt]}
    assert not pending_questions([summary])


@pytest.mark.asyncio
async def test_waiting_child_does_not_stop_independent_parent_work():
    class Child:
        config = SimpleNamespace(name="worker")
        session_id = "child"

        async def run_stream(self, task, context):
            yield {
                "code": 10,
                "content": json.dumps(
                    {"type": "ask_user", "question_id": "q", "question": "Format?", "options": []}
                ),
            }

    class Delegate(BusinessTool):
        name = "sub_agent"

        async def __call__(self, params):
            return json.dumps(await run_delegation(Child(), "task", "", "a"))

    work = BusinessTool()

    def script(turn, tools, input):
        if turn == 1:
            return [
                ResponseFunctionToolCall(
                    type="function_call", call_id="child", name="sub_agent", arguments="{}"
                )
            ]
        if turn == 2:
            return [
                ResponseFunctionToolCall(
                    type="function_call", call_id="work", name="business", arguments="{}"
                )
            ]
        return [final_message()]

    agent = Agent(
        name="parent",
        model=ScriptedModel(script),
        tools=convert_tools([Delegate(), work]),
        tool_use_behavior=question_tool_behavior(),
    )
    result = Runner.run_streamed(agent, input="two tasks", run_config=RunConfig(tracing_disabled=True))
    async for _ in _stream_sdk_events(result, []):
        pass
    assert work.calls == 1


def test_delegation_material_is_a_snapshot_with_separate_origins():
    attachments = {
        "images": ["https://example.com/current.png"],
        "files": [{"name": "rules.txt", "content": "full text"}],
    }
    history = [{"role": "tool", "content": json.dumps({"image_path": "https://example.com/prior.png"})}]
    snapshot = delegation_snapshot("Use this image", history, attachments)
    attachments["files"][0]["content"] = "changed later"
    rendered = render_delegation_context(snapshot, "Additional model notes")
    assert snapshot["current_attachments"]["files"][0]["content"] == "full text"
    assert snapshot["prior_assets"][0]["url"] == "https://example.com/prior.png"
    assert "current_user_input" in rendered and "Parent model notes" in rendered


@pytest.mark.asyncio
async def test_explicit_delegation_receives_current_material(workspace, monkeypatch):
    import core.sub_agent as sub

    contexts = []

    class Child:
        def __init__(self, **kwargs):
            self.config = SimpleNamespace(name="worker")
            self.session_id = kwargs["session_id"]

        async def run_stream(self, task, context):
            contexts.append(context)
            yield {"code": 2, "content": "done"}

    monkeypatch.setattr(sub, "SubAgentRunner", Child)
    snapshot = delegation_snapshot("Original user words", [], {"images": ["https://example.com/current.png"]})
    tool = SubAgentTool("alice", "main", workspace=workspace, context_fn=lambda: snapshot)
    await tool.delegate(SubAgentParams(task="Model task", agent_type="worker", context="Model notes"))
    assert (
        "Original user words" in contexts[0] and "current.png" in contexts[0] and "Model notes" in contexts[0]
    )


@pytest.mark.asyncio
async def test_error_only_compatibility_result_is_preserved():
    class ErrorTool(BusinessTool):
        async def __call__(self, params):
            return ToolResult(output="", error="Unavailable")

    output = await convert_tool(ErrorTool()).on_invoke_tool(RunContextWrapper(context=None), "{}")
    observation = tool_observation(output)
    assert observation["status"] == "error" and observation["error"] == "Unavailable"


@pytest.mark.asyncio
async def test_receipt_uses_typed_artifacts_and_checks_actual_delivery():
    class Child:
        config = SimpleNamespace(name="worker")
        session_id = "child"

        async def run_stream(self, task, context):
            yield {
                "code": 5,
                "content": json.dumps(
                    {
                        "name": "files",
                        "status": "success",
                        "artifacts": [
                            {"url": "https://example.com/final.pdf"},
                            {"url": "https://example.com/intermediate.pdf"},
                        ],
                    }
                ),
            }
            yield {
                "code": 2,
                "content": "Source: https://example.com/source. Final: https://example.com/final.pdf",
            }

    receipt = await run_delegation(Child(), "task", "", "a")
    assert receipt["artifact_refs"] == ["https://example.com/final.pdf"]
    assert [item["delivery_status"] for item in receipt["artifacts"]] == ["delivered", "produced"]


@pytest.mark.asyncio
async def test_answer_resumes_the_saved_worker_before_skill_matching(workspace, monkeypatch):
    import core.sub_agent as sub

    question = {"question_id": "question-a", "question": "Which format?", "options": []}
    receipt = {
        "type": "sub_agent_receipt",
        "owner": "worker",
        "task_id": "report-a",
        "status": "waiting_user",
        "pending_questions": [question],
    }
    workspace.history_save(
        "main",
        {
            "conversation": [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "child", "function": {"name": "sub_agent", "arguments": "{}"}}],
                },
                {
                    "role": "tool",
                    "name": "sub_agent",
                    "tool_call_id": "child",
                    "content": json.dumps(receipt),
                },
            ]
        },
    )
    observed = []

    class Child:
        def __init__(self, **kwargs):
            self.config = SimpleNamespace(name="worker")
            self.session_id = "sa_worker_" + kwargs["session_id"]

        async def run_stream(self, task, context):
            observed.append((self.session_id, self.answer_to, task, context))
            yield {"code": 2, "content": "Prepared the chosen format"}

    monkeypatch.setattr(sub, "SubAgentRunner", Child)
    agent = EvoAgent(
        "alice",
        "main",
        plugins=[],
        workspace=workspace,
        system_prompt="Offline",
        answer_to={"question_id": "question-a"},
    )
    agent.skill_matcher.match = AsyncMock(
        side_effect=AssertionError("A saved answer must route before matching")
    )
    agent._create_sdk_agent = AsyncMock(
        side_effect=AssertionError("No parent model needed for answer routing")
    )
    events = [event async for event in agent.run_stream("PDF")]
    assert observed[0][:3] == (
        "sa_worker_" + task_session_base("main", "report-a"),
        {"question_id": "question-a"},
        "PDF",
    )
    assert "current_user_input" in observed[0][3] and "Which format?" in observed[0][3]
    assert any(event.get("task_id") == "report-a" for event in events)
    assert "question-a" not in pending_questions(workspace.history_load("main")["conversation"])


def test_frontend_question_answers_keep_question_and_task_identity():
    if not shutil.which("node"):
        pytest.skip("Node.js required")
    html = Path("app/static/index.html").read_text()
    functions = (
        "function questionAnswerRoute"
        + html.split("function questionAnswerRoute", 1)[1].split("async function send()", 1)[0]
    )
    script = """
class Element {
  constructor(){this.children=[];this.dataset={};this.handlers={};}
  appendChild(child){this.children.push(child);return child;}
  querySelector(){return null;}
  querySelectorAll(){return this.children.filter(child=>child.className==='ask-panel');}
  addEventListener(event,handler){this.handlers[event]=handler;}
}
global.document={createElement:()=>new Element()};
global.sending=false; global.inputEl={dataset:{}}; global.scrollToBottom=()=>{};
const answers=[];global.useCard=text=>answers.push({text,route:JSON.parse(inputEl.dataset.answerTo)});
const owner=new Element();
for(const id of ['a','b']) renderAskPanel(owner,'Format?',[{label:'PDF'}],questionAnswerRoute(
 {question_id:'q-'+id},{task_id:id,source_agent:'worker'}));
owner.children[0].children[1].handlers.click();
const second=owner.children[1];second.children[2].value='Word';second.children[3].handlers.click();
process.stdout.write(JSON.stringify(answers));
"""
    result = subprocess.run(["node", "-e", functions + script], text=True, capture_output=True, check=True)
    answers = json.loads(result.stdout)
    assert [(row["text"], row["route"]["question_id"], row["route"]["task_id"]) for row in answers] == [
        ("PDF", "q-a", "a"),
        ("Word", "q-b", "b"),
    ]


@pytest.mark.asyncio
async def test_tool_outcomes_do_not_confuse_different_calls_with_a_retry():
    class Child:
        config = SimpleNamespace(name="worker")
        session_id = "child"

        async def run_stream(self, task, context):
            for call_id, result in (
                ("a", {"success": False, "error": "One file failed"}),
                ("b", {"success": True}),
            ):
                yield {
                    "code": 5,
                    "content": json.dumps({"name": "files", "id": call_id, "result": json.dumps(result)}),
                }
            yield {"code": 2, "content": "One file completed; another failed."}

    receipt = await run_delegation(Child(), "task", "", "a")
    assert [row["status"] for row in receipt["tool_outcomes"]] == ["error", "success"]
    assert receipt["tool_outcomes"][0]["error"] == "One file failed"


@pytest.mark.asyncio
async def test_artifact_metadata_is_captured_before_display_truncation():
    url = "https://example.com/final.pdf?signature=keep-this-exactly"

    class Files(BusinessTool):
        async def __call__(self, params):
            return json.dumps({"success": True, "details": "long text " * 500, "file_url": url})

    def script(turn, tools, input):
        if turn == 1:
            return [
                ResponseFunctionToolCall(
                    type="function_call", call_id="files", name="business", arguments="{}"
                )
            ]
        return [final_message(url)]

    tools = [Files()]
    agent = Agent(name="files", model=ScriptedModel(script), tools=convert_tools(tools))
    result = Runner.run_streamed(agent, input="file", run_config=RunConfig(tracing_disabled=True))
    events = [event async for event in _stream_sdk_events(result, tools)]
    output = json.loads(next(event["content"] for event in events if event["code"] == 5))
    assert url not in output["result"] and output["artifacts"][0]["url"] == url


@pytest.mark.asyncio
async def test_question_identity_survives_compression_and_answer_removes_it():
    question = {"type": "ask_user", "question_id": "q", "question": "Format?", "options": []}
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="<summary>Waiting for a report format.</summary>"),
            )
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )
    compressor = HistoryCompressor(async_client=client, max_history_tokens=64, min_messages_to_keep=0)
    history = [
        {"role": "user", "content": "Full evidence " * 1000},
        {"role": "tool", "content": json.dumps(question)},
    ]
    compressed = await compressor.compress_history(history)
    assert compressed is not history and compressed[0]["pending_questions"][0]["question_id"] == "q"
    answer = resolve_answer(compressed, {"question_id": "q"})
    compressed.append(user_message("PDF", answer))
    second = await compressor.compress_history(compressed)
    assert "q" not in pending_questions(second)


@pytest.mark.asyncio
async def test_unknown_question_produces_an_error_without_starting_a_model(workspace):
    agent = EvoAgent(
        "alice",
        "missing-question",
        plugins=[],
        workspace=workspace,
        system_prompt="Offline",
        answer_to={"question_id": "missing"},
    )
    agent._create_sdk_agent = AsyncMock(side_effect=AssertionError("Must not start a model"))
    events = [event async for event in agent.run_stream("PDF")]
    assert events[0]["code"] == 0 and "no longer pending" in events[0]["content"]
    assert workspace.history_load("missing-question") is None


def test_interrupted_tool_outcome_does_not_invent_a_business_failure():
    observation = tool_observation({"status": "interrupted", "outcome": "unknown"})
    assert observation["status"] == "unknown" and not observation["is_error"]


@pytest.mark.asyncio
async def test_invalid_question_can_be_corrected_before_the_run_stops():
    def script(turn, tools, input):
        assert turn <= 2
        args = {"question": "Format?"} if turn == 1 else {"question": "Format?", "options": []}
        return [
            ResponseFunctionToolCall(
                type="function_call",
                call_id=f"ask-{turn}",
                name="ask_user_question",
                arguments=json.dumps(args),
            )
        ]

    tool = AskUserQuestionTool()
    model = ScriptedModel(script)
    agent = Agent(
        name="question", model=model, tools=convert_tools([tool]), tool_use_behavior=question_tool_behavior()
    )
    result = Runner.run_streamed(agent, input="Ask me", run_config=RunConfig(tracing_disabled=True))
    events = [event async for event in _stream_sdk_events(result, [tool])]
    assert model.turn == 2 and sum(event["code"] == 10 for event in events) == 1


@pytest.mark.asyncio
async def test_mcp_failure_channels_are_explicit_and_cancellation_propagates():
    import asyncio

    from core.mcp.adapter import _make_invoker

    consumer = SimpleNamespace(call_tool=AsyncMock(side_effect=RuntimeError("unavailable")))
    output = await _make_invoker(consumer, "probe", 1)(RunContextWrapper(context=None), "{}")
    assert tool_observation(output)["status"] == "error"
    consumer.call_tool = AsyncMock(
        return_value={"isError": True, "content": [{"type": "text", "text": "Failed"}]}
    )
    output = await _make_invoker(consumer, "probe", 1)(RunContextWrapper(context=None), "{}")
    assert tool_observation(output)["error"] == "Failed"
    consumer.call_tool = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _make_invoker(consumer, "probe", 1)(RunContextWrapper(context=None), "{}")
