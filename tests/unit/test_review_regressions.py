"""Review regressions across streamed delivery, answer identity and provider usage."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, RunConfig, Runner
from openai.types.responses import ResponseCompletedEvent, ResponseFunctionToolCall

from configs import settings
from core.agent import EvoAgent
from core.agent_compat import convert_tools
from core.delegation import run_delegation
from core.runtime.delegation_context import request_input
from core.runtime.execution import SDKRunSession
from core.runtime.history import _history_to_sdk_input
from core.runtime.questions import pending_questions, resolve_answer, user_message
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from tests.unit.test_runtime_contracts import BusinessTool
from tests.unit.test_usage import MeasuredModel
from utils.context_recovery import collect_assets
from utils.history_compressor import HistoryCompressor
from utils.storage_backend.localfs import LocalFSBackend
from utils.token_counter import count_tokens
from utils.tool_results import tool_observation
from utils.usage import usage_scope
from utils.workspace import Workspace


@pytest.mark.asyncio
async def test_streamed_artifact_url_keeps_its_identity_across_chunks():
    url = "https://example.com/report.pdf?signature=exact"

    class Child:
        config = SimpleNamespace(name="worker")
        session_id = "child"

        async def run_stream(self, task, context):
            yield {
                "code": 5,
                "content": json.dumps({"name": "report", "status": "success", "artifacts": [{"url": url}]}),
            }
            for chunk in ("Download: ", url[:23], url[23:35], url[35:]):
                yield {"code": 2, "content": chunk}

    receipt = await run_delegation(Child(), "report", "", "report-a")
    assert receipt["artifact_refs"] == [url]
    assert receipt["artifacts"][0]["delivery_status"] == "delivered"


def receipt(question_id=None, *, status="waiting_user", task_id="a"):
    data = {"type": "sub_agent_receipt", "owner": "worker", "task_id": task_id, "status": status}
    if question_id:
        data["pending_questions"] = [{"question_id": question_id, "question": "Format?", "options": []}]
    return {"role": "tool", "name": "sub_agent", "content": json.dumps(data)}


def test_new_receipts_supersede_old_question_cards_only_for_the_same_task():
    history = [receipt("old"), receipt("other", task_id="b"), receipt("replacement")]
    assert set(pending_questions(history)) == {"replacement", "other"}
    history.append(receipt(status="delivered"))
    assert set(pending_questions(history)) == {"other"}
    with pytest.raises(ValueError, match="no longer pending"):
        resolve_answer(history, {"question_id": "old"})


def own_questions():
    history = []
    for number, text in (("a", "Which source?"), ("b", "Which output format?")):
        history.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": number, "function": {"name": "ask_user_question", "arguments": "{}"}}
                    ],
                },
                {
                    "role": "tool",
                    "name": "ask_user_question",
                    "tool_call_id": number,
                    "content": json.dumps(
                        {"type": "ask_user", "question_id": number, "question": text, "options": []}
                    ),
                },
            ]
        )
    return history


def test_answer_identity_is_visible_to_the_model_and_summary():
    history = own_questions()
    answer = resolve_answer(history, {"question_id": "b"})
    message = user_message("PDF", answer)
    sdk_input = _history_to_sdk_input(history + [message])
    assert '"question_id": "b"' in sdk_input[-1]["content"]
    assert "Which output format?" in sdk_input[-1]["content"]
    assert '"question_id": "b"' in HistoryCompressor._render_message_for_summary(message)
    assert message["content"] == "PDF"
    assert HistoryCompressor().calculate_history_tokens([message]) == count_tokens(
        json.dumps(_history_to_sdk_input([message]), ensure_ascii=False)
    )


@pytest.mark.asyncio
async def test_local_answer_continues_without_repeating_skill_matching(tmp_path, monkeypatch):
    import core.agent as main

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    workspace.history_save("main", {"conversation": own_questions()})

    def script(turn, tools, input):
        assert '"question_id": "b"' in input[-1]["content"]
        return [final_message("Prepared the requested format")]

    agent = EvoAgent(
        "alice",
        "main",
        plugins=[],
        workspace=workspace,
        system_prompt="Offline",
        model=ScriptedModel(script),
        answer_to={"question_id": "b"},
    )
    agent.skill_matcher.match = AsyncMock(
        side_effect=AssertionError("An answer should continue the existing task")
    )
    events = [event async for event in agent.run_stream("PDF")]
    assert any(event.get("code") == 2 for event in events)
    assert set(pending_questions(workspace.history_load("main")["conversation"])) == {"a"}


@pytest.mark.asyncio
async def test_reused_provider_response_ids_do_not_drop_measured_usage():
    class ReusedIDModel(MeasuredModel):
        async def stream_response(self, *args, **kwargs):
            async for event in super().stream_response(*args, **kwargs):
                if isinstance(event, ResponseCompletedEvent):
                    event.response.id = "provider-reused-id"
                yield event

    def script(turn, tools, input):
        if turn == 1:
            return [
                ResponseFunctionToolCall(
                    type="function_call", call_id="work", name="business", arguments="{}"
                )
            ]
        return [final_message()]

    tools = [BusinessTool()]
    with usage_scope() as collector:
        agent = Agent(name="probe", model=ReusedIDModel(script), tools=convert_tools(tools))
        session = SDKRunSession(agent, HistoryCompressor(compression_level="none"), [], tools)
        session.start(
            lambda *args, **kwargs: Runner.run_streamed(
                *args, **kwargs, run_config=RunConfig(tracing_disabled=True)
            ),
            agent,
            input="work",
            max_turns=3,
        )
        async for _ in session.stream():
            pass
        assert len(session.result.raw_responses) == 2
        assert collector.snapshot()["usage"]["requests"] == 2
        assert collector.snapshot()["usage"]["total_tokens"] == 36


@pytest.mark.parametrize("structured", [True, False])
def test_file_source_urls_are_not_misclassified_as_uploaded_images(structured):
    image, file, source = (
        "https://example.com/input.png",
        "https://example.com/rules.txt",
        "https://example.com/docs",
    )
    images, files = [image], [{"url": file, "content": "Read " + source}]
    message = {"role": "user", "content": request_input("Use these inputs", images, files)}
    if structured:
        message["attachments"] = {"images": images, "files": files}
    refs = {ref["url"]: ref for ref in collect_assets([message])}
    assert refs[image]["kind"] == "upload" and refs[image]["field"] == "image"
    assert refs[file]["kind"] == "upload" and refs[file]["field"] == "file"
    assert refs[source]["kind"] == "source"


def test_explicit_unknown_outcome_is_not_promoted_to_success():
    result = tool_observation({"status": "unknown", "file_url": "https://example.com/unconfirmed.pdf"})
    assert result["status"] == "unknown" and not result["artifacts"]
