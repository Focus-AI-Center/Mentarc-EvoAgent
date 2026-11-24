"""Worker restrictions cover initial, skill-granted and discovered tool schemas."""

import json

import pytest
from agents import Agent, FunctionTool
from agents.run_context import RunContextWrapper

from configs import settings
from core.agent_compat import convert_tools
from core.plugins.internal import MemoryTool, SkillTool
from core.runtime.tool_policy import DELEGATED_POLICY, WORKER_POLICY
from core.skills import Skill, skill_to_md
from core.skills.manager import SkillManager
from core.skills.tool_mounting import SkillToolMounter
from core.tool_search import configure_agent_tools
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.mark.asyncio
async def test_read_only_actions_are_restricted_in_schema_and_invocation(tmp_path):
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    tools = convert_tools(
        [
            SkillTool(user_id="alice", workspace=workspace, skill_manager=SkillManager(workspace)),
            MemoryTool(user_id="alice", workspace=workspace),
        ],
        policy=WORKER_POLICY,
    )
    ctx = RunContextWrapper(context=None)
    for tool in tools:
        assert (
            set(tool.params_json_schema["properties"]["action"]["enum"]) == WORKER_POLICY.actions[tool.name]
        )
        action = "propose" if tool.name == "skill" else "append"
        result = json.loads(await tool.on_invoke_tool(ctx, json.dumps({"action": action})))
        assert result["status"] == "error"
    assert not workspace.proposal_list()
    assert "skill-creator" in await tools[0].on_invoke_tool(ctx, '{"action":"list"}')
    assert json.loads(await tools[0].on_invoke_tool(ctx, "{}"))["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("search_enabled", [True, False])
@pytest.mark.parametrize("policy", [WORKER_POLICY, DELEGATED_POLICY])
async def test_discovery_and_skill_restore_cannot_restore_forbidden_tools(
    tmp_path, monkeypatch, search_enabled, policy
):
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", search_enabled)
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    workspace.skill_set(
        "policy-test",
        skill_to_md(
            Skill(
                name="policy-test",
                description="Restore a workflow",
                required_tools=["ask_user_question", "update_user_profile", "execute_code"],
            )
        ),
    )
    native = [SkillTool(user_id="alice", workspace=workspace, skill_manager=SkillManager(workspace))]
    mounter = SkillToolMounter(
        native, user_id="alice", session_id="child", workspace=workspace, policy=policy
    )

    async def invoke(ctx, args):
        return "Read result"

    def tool(name):
        return FunctionTool(
            name=name,
            description=name,
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    async def discover(excluded):
        return [tool("ask_user_question"), tool("update_user_profile"), tool("mcp__files__read")]

    agent = Agent(name="child", instructions="Offline", tools=convert_tools(native, policy=policy))
    catalog = await configure_agent_tools(agent, loader=discover, policy=policy)
    await mounter.bind(agent, catalog)
    await mounter.on_loaded("policy-test")
    if catalog:
        await catalog.discover()
    names = {tool.name for tool in agent.tools}
    assert "execute_code" in names and "mcp__files__read" in names
    assert names.isdisjoint(policy.denied)
    assert {tool.name for tool in native}.isdisjoint(policy.denied)


def test_worker_files_and_task_identity_survive_repeated_compression():
    from utils.context_recovery import render_index
    from utils.history_compressor import HistoryCompressor

    receipt = {
        "type": "worker_receipt",
        "owner": "worker",
        "task_id": "research",
        "status": "completed",
        "output_dir": "workers/thread/run",
        "files": ["workers/thread/run/response.md"],
        "summary": "Long body",
    }
    history = [
        {
            "role": "tool",
            "name": "worker_status",
            "content": json.dumps({"finished": [receipt], "running": []}),
        }
    ]
    receipts = HistoryCompressor.delegation_receipts(history)
    text, _ = render_index(history, receipts, [], 4000)
    assert "workers/thread/run" in text and "research" in text
    assert "Long body" not in text
    again = [{"role": "user", "content": "<summary>Done</summary>" + text, "delegation_receipts": receipts}]
    assert HistoryCompressor.delegation_receipts(again)[0]["output_dir"] == receipt["output_dir"]
