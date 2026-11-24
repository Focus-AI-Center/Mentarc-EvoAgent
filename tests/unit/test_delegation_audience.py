"""Audience follows the invocation route, independently of the agent profile."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from openai.types.responses import ResponseFunctionToolCall

from configs import settings
from core.agent import EvoAgent
from core.agent_compat import convert_tools
from core.runtime.delegation_context import DELEGATED_AGENT_INSTRUCTION
from core.skills import Skill
from core.sub_agent import AgentTypeConfig, SubAgentRunner
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", LocalFSBackend(tmp_path))


def call(name, arguments):
    return ResponseFunctionToolCall(
        type="function_call", name=name, arguments=json.dumps(arguments), call_id=name
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [True, False])
async def test_same_profile_has_different_audience_and_tool_policy(workspace, delegated):
    runner = SubAgentRunner(
        AgentTypeConfig(name="analyst", body="Analyze the supplied material."),
        "alice",
        "session",
        workspace=workspace,
        delegated=delegated,
        output_dir="workers/task/attempt" if delegated else None,
    )
    assert (DELEGATED_AGENT_INSTRUCTION in await runner._build_system_prompt()) is delegated
    tools = {tool.name: tool for tool in convert_tools(runner._build_tools(), policy=runner.policy)}
    assert ("ask_user_question" in tools) is not delegated
    actions = tools["skill"].params_json_schema["properties"]["action"]["enum"]
    assert ("propose" in actions) is not delegated
    # Analytical agents keep their authorized retention tools in delegated mode.
    assert {"memory", "update_user_profile", "workspace_file"} <= tools.keys()


@pytest.mark.asyncio
async def test_worker_and_skill_handoff_receive_the_same_internal_audience_contract(workspace):
    worker = SubAgentRunner(
        AgentTypeConfig(name="worker", execution_mode="worker", body="Execute a task."),
        "alice",
        "session",
        workspace=workspace,
        internal_worker=True,
        output_dir="workers/task/attempt",
    )
    handoff = SubAgentRunner.from_skill(
        Skill(name="analysis", description="Analysis workflow", instructions="Analyze input."),
        "alice",
        "session",
        workspace=workspace,
        delegated=True,
        output_dir="workers/task/handoff",
    )
    assert DELEGATED_AGENT_INSTRUCTION in await worker._build_system_prompt()
    assert DELEGATED_AGENT_INSTRUCTION in await handoff._build_system_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_parent_reads_child_files_and_is_the_only_user_facing_speaker(
    workspace, monkeypatch, automatic
):
    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    workspace.agent_set("analyst", "---\nname: analyst\n---\nAnalyze the input.")

    def child_script(turn, tools, input):
        assert "ask_user_question" not in {tool.name for tool in tools}
        return [final_message("Internal analysis for the parent only.")]

    child_model = ScriptedModel(child_script)
    monkeypatch.setattr(
        sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": child_model}))
    )

    def parent_script(turn, tools, input):
        if not automatic and turn == 1:
            return [call("sub_agent", {"agent_type": "analyst", "task": "Analyze", "task_id": "analysis"})]
        receipts = [
            json.loads(item["output"])
            for item in input
            if item.get("type") == "function_call_output" and '"sub_agent_receipt"' in str(item.get("output"))
        ]
        assert len(receipts) == 1 and receipts[0]["status"] == "completed"
        assert receipts[0]["delivery_complete"] is False
        if turn == (1 if automatic else 2):
            return [call("workspace_file", {"action": "read", "path": receipts[0]["files"][0]})]
        assert "Internal analysis for the parent only." in json.dumps(input)
        return [final_message("Main agent verified delivery.")]

    parent = EvoAgent(
        "alice",
        "main",
        plugins=[],
        workspace=workspace,
        system_prompt="Offline",
        model=ScriptedModel(parent_script),
    )
    if automatic:
        skill = Skill(
            name="analysis", description="Analysis", requires_sub_agent=True, sub_agent_name="analyst"
        )
        parent.skill_matcher.match = AsyncMock(
            return_value=SimpleNamespace(skill=skill, match_type="trigger", confidence=1.0)
        )
    else:
        parent.skill_matcher.match = AsyncMock(return_value=SimpleNamespace(skill=None))
    events = [event async for event in parent.run_stream("Analyze supplied material")]
    assert (
        "".join(event["content"] for event in events if event["code"] == 2) == "Main agent verified delivery."
    )
    assert not any(event["code"] in (8, 10, 12) for event in events)
    results = [json.loads(event["content"]) for event in events if event["code"] == 5]
    for result in results:
        if result.get("name") == "sub_agent":
            assert json.loads(result["result"])["files"]
    saved = workspace.history_load("main")["conversation"]
    assert sum(message["role"] == "user" for message in saved) == 1
    assert child_model.turn == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ask", [False, True])
async def test_direct_sub_agent_api_keeps_user_replies_and_questions(workspace, monkeypatch, ask):
    import core.agent as main
    import core.sub_agent as sub
    from app.api.routers import agent, sub_agent

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent, "_create_workspace", AsyncMock(return_value=workspace))
    workspace.agent_set("analyst", "---\nname: analyst\n---\nAnalyze the input.")

    def script(turn, tools, input):
        assert "ask_user_question" in {tool.name for tool in tools}
        if ask:
            return [
                call("ask_user_question", {"question": "Which format?", "options": [{"label": "Markdown"}]})
            ]
        return [final_message("Direct analyst response.")]

    model = ScriptedModel(script)
    monkeypatch.setattr(sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": model})))
    app = FastAPI()
    app.include_router(sub_agent.router, prefix="/api/sub-agent")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/api/sub-agent/analyst/chat",
            json={"userId": "alice", "sessionId": "direct", "data": {"input": "Analyze"}},
        )
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    if ask:
        assert any(event["code"] == 10 for event in events)
    else:
        assert (
            "".join(event["content"] for event in events if event["code"] == 2) == "Direct analyst response."
        )
    assert model.turn == 1
