"""Offline worker scheduling, artifact access, deadlines and SDK integration."""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agents import FunctionTool
from fastapi import FastAPI
from openai.types.responses import ResponseFunctionToolCall

from configs import settings
from core.agent import EvoAgent
from core.agent_compat import BaseTool
from core.plugins.workers import WorkerParams, WorkerStatusParams, WorkerStatusTool, WorkerTool
from core.plugins.workspace_files import WorkspaceFileParams, WorkspaceFileTool
from core.runtime.workers import WorkerRegistry
from tests.unit import test_agent_registry
from tests.unit.test_agent_coordination import ScriptedModel, final_message
from tests.unit.test_interrupted_history import PausedTool, consume, model_for_calls
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace

dual_workspace = test_agent_registry.workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", LocalFSBackend(tmp_path))


@pytest.fixture
def registry(workspace):
    return WorkerRegistry(workspace, "main")


@pytest.mark.asyncio
async def test_dispatch_returns_before_completion_and_result_is_a_stored_file(
    workspace, registry, monkeypatch
):
    started, gate = asyncio.Event(), asyncio.Event()
    tool = WorkerTool("alice", "main", workspace, registry)
    full = "Substantial source-backed analysis.\n" * 1000

    async def execute(job, *args):
        started.set()
        await gate.wait()
        job.text.append(full)
        return "completed"

    monkeypatch.setattr(tool, "_execute", execute)
    receipt = json.loads(await tool(WorkerParams(task="Analyze", task_id="analysis")))
    await started.wait()
    assert receipt["status"] == "running" and not registry.jobs["analysis"].finished
    gate.set()
    result = await registry.collect(["analysis"], 1)
    final = result["finished"][0]
    assert final["status"] == "completed" and len(final["summary"]) <= 1500
    assert workspace.file_read(final["files"][0]) == full
    assert (await registry.collect(["analysis"]))["finished"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("search_enabled", [True, False])
async def test_worker_inherits_configured_plugins_and_mcp_with_independent_tool_state(
    workspace, monkeypatch, search_enabled
):
    import core.agent as main
    import core.sub_agent as sub
    from core.plugins import PLUGIN_CLASSES

    instances, discovered = [], []

    class Probe(BaseTool):
        name, description, param_anno = "worker_probe", "Probe inherited capability", WorkerStatusParams

        def __init__(self, **kwargs):
            self.kwargs, self.calls = kwargs, 0
            instances.append(self)

        async def __call__(self, params):
            self.calls += 1
            return "Inherited tool executed"

    async def discover(manager, excluded):
        discovered.append((manager.user_id, manager.session_id, manager.workspace))

        async def invoke(ctx, args):
            return "Inherited MCP executed"

        return [
            FunctionTool(
                name=name,
                description="MCP source reader",
                params_json_schema={"type": "object", "properties": {}},
                on_invoke_tool=invoke,
            )
            for name in ("mcp__sources__read", "ask_user_question")
        ]

    monkeypatch.setitem(PLUGIN_CLASSES, "worker_probe", Probe)
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", search_enabled)
    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", discover)
    operations = ([call("toolsearch", {"query": "worker_probe"})] if search_enabled else []) + [
        call("worker_probe", {})
    ]
    if search_enabled:
        operations.append(call("toolsearch", {"query": "mcp__sources__read"}))
    operations.append(call("mcp__sources__read", {}))

    def script(turn, tools, input):
        names = {tool.name for tool in tools}
        assert names.isdisjoint({"ask_user_question", "worker", "update_user_profile"})
        if search_enabled and turn == 1:
            assert "worker_probe" not in names
        return [operations[turn - 1]] if turn <= len(operations) else [final_message("Saved findings")]

    model = ScriptedModel(script)
    monkeypatch.setattr(sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": model})))
    parent = EvoAgent(
        "alice", "parent", plugins=["worker_probe"], workspace=workspace, system_prompt="Offline"
    )
    worker = next(tool for tool in parent.tools if tool.name == "worker")
    await worker(WorkerParams(task="Use the inherited source readers", task_id="inherit"))
    # A dispatch snapshots configuration, rather than sharing the parent's mutable list.
    parent.plugin_names.clear()
    receipt = (await worker.registry.collect(["inherit"], 3))["finished"][0]
    assert receipt["status"] == "completed"
    assert len(instances) == 2 and instances[0].calls == 0 and instances[1].calls == 1
    assert instances[1].kwargs["workspace_cwd"] == receipt["output_dir"]
    assert instances[1].kwargs["session_id"] == "parent"
    assert discovered == [("alice", "parent", workspace)]
    assert [call["tool"] for call in receipt["tool_calls"]].count("worker_probe") == 1
    assert all(call["status"] == "completed" for call in receipt["tool_calls"])
    frames = [json.loads(event[1]["content"]) for event in list(worker.registry.events._queue)]
    states = [json.loads(frame["result"]) for frame in frames]
    assert len({frame["id"] for frame in frames}) == 1
    assert frames[-1]["streaming"] is False
    assert states[-1]["files"] == receipt["files"] and states[-1]["tool_calls"]
    assert all("Saved findings" not in frame["result"] for frame in frames)


@pytest.mark.asyncio
async def test_worker_artifact_downloads_are_read_only_attachments_on_both_backends(
    dual_workspace, monkeypatch
):
    from app.api.routers import workspace as routes

    path = "workers/task/attempt/data.bin"
    content = b"\x00\xffbinary"
    dual_workspace._backend.write_bytes_sync("alice/" + path, content)
    monkeypatch.setattr(routes, "Workspace", lambda user: Workspace(user, dual_workspace._backend))
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/workspace")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/workspace/alice/worker-files/" + path)
        assert response.content == content
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get("/api/workspace/bob/worker-files/" + path)).status_code == 404
        for invalid in ("prompts/USER.md", "workers/%2e%2e/prompts/USER.md", "workers/a%5csecret"):
            assert (await client.get("/api/workspace/alice/worker-files/" + invalid)).status_code == 400
        assert (await client.put("/api/workspace/alice/worker-files/" + path)).status_code == 405


@pytest.mark.asyncio
async def test_worker_progress_distinguishes_attempts_and_unconfirmed_tools(registry):
    async def run(job):
        job.tool_calls.append({"tool": "execute_code", "call_id": "code", "status": "running"})
        job.text.append("Partial result")
        return "cancelled"

    first = registry.launch("retry", "First", "workers/task/first", run, timeout=1)
    await first.task
    assert first.receipt["tool_calls"][0]["status"] == "unconfirmed"
    await registry.collect(["retry"])
    second = registry.launch("retry", "Second", "workers/task/second", run, timeout=1)
    await second.task
    ids = {json.loads(event[1]["content"])["id"] for event in list(registry.events._queue)}
    assert ids == {"worker-progress-workers/task/first", "worker-progress-workers/task/second"}


@pytest.mark.asyncio
async def test_unknown_ids_do_not_collect_or_report_other_jobs(registry):
    async def run(job):
        job.text.append("Completed")

    job = registry.launch("known", "Known", "workers/known/run", run, timeout=1)
    await job.task
    result = json.loads(
        await WorkerStatusTool(registry)(WorkerStatusParams(task_ids=["missing"], wait_seconds=0))
    )
    assert result["status"] == "error" and not job.reported
    assert (await registry.collect(["known"]))["finished"][0]["task_id"] == "known"


@pytest.mark.asyncio
async def test_waiting_does_not_cancel_and_a_running_id_cannot_be_dispatched_twice(registry):
    gate = asyncio.Event()

    async def run(job):
        await gate.wait()

    job = registry.launch("a", "A", "workers/a/run", run, timeout=1)
    with pytest.raises(ValueError, match="running"):
        registry.launch("a", "Again", "workers/a/again", run, timeout=1)
    result = await registry.collect(["a"], 0.01)
    assert result["running"] and not job.task.cancelled()
    gate.set()
    await job.task


@pytest.mark.asyncio
async def test_queueing_counts_towards_timeout_and_concurrency_is_bounded(workspace, monkeypatch):
    monkeypatch.setattr(settings, "WORKER_MAX_CONCURRENCY", 1)
    registry = WorkerRegistry(workspace, "main")
    gate, started = asyncio.Event(), asyncio.Event()
    seen = []

    async def run(job):
        seen.append(job.task_id)
        started.set()
        await gate.wait()

    first = registry.launch("a", "A", "workers/a/run", run, timeout=1)
    await started.wait()
    second = registry.launch("b", "B", "workers/b/run", run, timeout=0.02)
    await second.task
    assert second.receipt["status"] == "timeout" and seen == ["a"]
    gate.set()
    await first.task


@pytest.mark.asyncio
async def test_close_before_task_starts_cannot_leave_running_state(registry):
    async def run(job):
        raise AssertionError("Cancelled before start")

    job = registry.launch("a", "A", "workers/a/run", run, timeout=1)
    await registry.close()
    assert job.finished and job.receipt["status"] == "cancelled"
    result = await registry.collect()
    assert not result["running"]


@pytest.mark.asyncio
async def test_failure_and_artifact_write_failure_are_terminal(workspace, registry, monkeypatch):
    async def fail(job):
        raise RuntimeError("Offline failure")

    job = registry.launch("a", "A", "workers/a/run", fail, timeout=1)
    await job.task
    assert job.receipt["status"] == "failed"

    async def write(job):
        job.text.append("A completed response")

    monkeypatch.setattr(workspace, "file_write", lambda *args: (_ for _ in ()).throw(OSError("failed")))
    other = registry.launch("b", "B", "workers/b/run", write, timeout=1)
    await other.task
    assert other.receipt["status"] == "failed" and other.finished


@pytest.mark.asyncio
async def test_file_tool_writes_only_the_assigned_output_and_parent_reads_it(workspace):
    worker = WorkspaceFileTool(workspace, output_dir="workers/a/run")
    parent = WorkspaceFileTool(workspace)
    out = json.loads(
        await worker(WorkspaceFileParams(action="write", path="workers/a/run/data.md", content="Full report"))
    )
    assert out["path"] == "workers/a/run/data.md"
    assert (
        json.loads(await parent(WorkspaceFileParams(action="read", path=out["path"])))["content"]
        == "Full report"
    )
    for path in ("prompts/USER.md", "workers/b/run/result.md", "workers/a/run/response.md"):
        denied = json.loads(await worker(WorkspaceFileParams(action="write", path=path, content="No")))
        assert denied["status"] == "error"
    with pytest.raises(ValueError):
        await worker(WorkspaceFileParams(action="write", path="workers/a/run/../../private.md", content="No"))
    assert (
        json.loads(
            await parent(WorkspaceFileParams(action="write", path="workers/a/run/data.md", content="No"))
        )["status"]
        == "error"
    )


def call(name, arguments, call_id=None):
    return ResponseFunctionToolCall(
        type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id or name
    )


def parent_for(workspace, monkeypatch, model):
    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    parent = EvoAgent("alice", "main", plugins=[], workspace=workspace, system_prompt="Offline", model=model)
    parent.skill_matcher.match = AsyncMock(return_value=SimpleNamespace(skill=None))
    return parent


@pytest.mark.asyncio
async def test_real_sdk_parent_continues_work_then_reads_files_without_another_user_message(
    workspace, monkeypatch
):
    started, gate = asyncio.Event(), asyncio.Event()
    params = WorkerStatusParams

    class Independent(BaseTool):
        name, description, param_anno = "independent", "Independent parent work", params

        async def __call__(self, params):
            await asyncio.wait_for(started.wait(), 1)
            gate.set()
            return "Parent completed its own work"

    def script(turn, tools, input):
        if turn == 1:
            return [call("worker", {"task": "Research", "task_id": "research"})]
        if turn == 2:
            assert not gate.is_set()
            return [call("independent", {})]
        if turn == 3:
            return [final_message("Initial parent response")]
        if turn == 4:
            outputs = [
                json.loads(i["output"])
                for i in input
                if i.get("type") == "function_call_output" and '"finished"' in str(i.get("output"))
            ]
            result = outputs[-1]["finished"][0]
            return [call("workspace_file", {"action": "read", "path": result["files"][0]})]
        assert "Verified research with sources" in json.dumps(input)
        return [final_message("Verified final delivery")]

    parent = parent_for(workspace, monkeypatch, ScriptedModel(script))
    parent.tools.append(Independent())
    worker = next(tool for tool in parent.tools if tool.name == "worker")

    async def execute(job, *args):
        started.set()
        await gate.wait()
        job.text.append("Verified research with sources")
        return "completed"

    monkeypatch.setattr(worker, "_execute", execute)
    events = [event async for event in parent.run_stream("Research and do independent work")]
    visible = "".join(event.get("content", "") for event in events if event["code"] == 2)
    assert "Verified final delivery" in visible and "Verified research with sources" not in visible
    assert sum(message.get("role") == "user" for message in parent.history) == 1
    assert all(job.finished and job.reported for job in parent._workers.jobs.values())
    saved = workspace.history_load("main")["conversation"]
    assert any(message.get("name") == "worker_status" for message in saved)


@pytest.mark.asyncio
async def test_parent_cancellation_saves_worker_history_and_finalizes_its_receipt(workspace, monkeypatch):
    import core.agent as main
    import core.sub_agent as sub

    child_pause, parent_pause = PausedTool(), PausedTool()

    def script(turn, tools, input):
        if turn == 1:
            return [call("worker", {"task": "Research", "task_id": "research"})]
        return [call("paused", {})]

    parent = parent_for(workspace, monkeypatch, ScriptedModel(script))
    parent.tools.append(parent_pause)
    monkeypatch.setattr(sub.SubAgentRunner, "_build_tools", lambda self: [child_pause])
    child_model = model_for_calls()
    monkeypatch.setattr(
        sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": child_model}))
    )
    task = asyncio.create_task(consume(parent.run_stream("Research")))
    await asyncio.wait_for(child_pause.started.wait(), 2)
    await asyncio.wait_for(parent_pause.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = parent._workers.jobs["research"]
    assert job.finished and job.receipt["status"] == "cancelled"
    child_saved = workspace.history_load(job.session_id)
    assert child_saved["interrupted"] and child_saved["execution_mode"] == "worker"
    saved = workspace.history_load("main")
    assert saved["interrupted"]
    receipts = [
        json.loads(message["content"]) for message in saved["conversation"] if message.get("name") == "worker"
    ]
    assert receipts[-1]["status"] == "cancelled"
    assert child_pause.stopped.is_set()


@pytest.mark.asyncio
async def test_deadline_applies_to_main_execution_after_worker_dispatch(workspace, monkeypatch):
    registry = WorkerRegistry(workspace, "main")
    registry.deadline = asyncio.get_running_loop().time() + 0.04
    registry.reply_deadline = registry.deadline

    async def run(job):
        await asyncio.Event().wait()

    registry.launch("a", "A", "workers/a/run", run, timeout=1)

    async def source():
        await asyncio.Event().wait()
        yield {}

    with pytest.raises(TimeoutError):
        async with aclosing(registry.stream(source())) as stream:
            async for _ in stream:
                pass
    await registry.close()


@pytest.mark.asyncio
async def test_reusing_task_id_preserves_child_history_and_uses_a_fresh_output_directory(
    workspace, monkeypatch
):
    import core.agent as main
    import core.sub_agent as sub

    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        sub,
        "_SDKAgent",
        lambda **kwargs: main._SDKAgent(
            **(kwargs | {"model": ScriptedModel(lambda *args: [final_message("Internal file result")])})
        ),
    )
    receipts = []
    for _ in range(2):
        registry = WorkerRegistry(workspace, "main")
        tool = WorkerTool("alice", "main", workspace, registry)
        await tool(WorkerParams(task="Continue", task_id="same", background=True))
        receipts.append((await registry.collect(["same"], 2))["finished"][0])
    assert receipts[0]["session_id"] == receipts[1]["session_id"]
    assert receipts[0]["output_dir"] != receipts[1]["output_dir"]
    history = workspace.history_load(receipts[0]["session_id"])["conversation"]
    assert sum(message.get("role") == "user" for message in history) == 2
    assert all(workspace.file_read(r["files"][0]) == "Internal file result" for r in receipts)
    assert not workspace.list_sessions()


@pytest.mark.asyncio
async def test_sdk_file_worker_uses_storage_without_starting_a_sandbox(dual_workspace, monkeypatch):
    import re

    import core.agent as main
    import core.plugins.execute_code as execution
    import core.sub_agent as sub

    monkeypatch.setattr(sub, "_ensure_sdk_client", lambda: None)
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    monkeypatch.setattr(main.MCPToolManager, "create_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        execution, "_get_backend", lambda: (_ for _ in ()).throw(AssertionError("No sandbox needed"))
    )

    def script(turn, tools, input):
        if turn == 1:
            directory = re.search(r"Write task outputs under `([^`]+)/`", json.dumps(input)).group(1)
            return [
                call(
                    "workspace_file",
                    {"action": "write", "path": directory + "/report.md", "content": "Sourced findings"},
                )
            ]
        return [final_message("Saved report with source references")]

    model = ScriptedModel(script)
    monkeypatch.setattr(sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": model})))
    registry = WorkerRegistry(dual_workspace, "main")
    tool = WorkerTool("alice", "main", dual_workspace, registry)
    await tool(WorkerParams(task="Read supplied material", task_id="text"))
    result = (await registry.collect(["text"], 2))["finished"][0]
    assert result["status"] == "completed"
    report = next(path for path in result["files"] if path.endswith("/report.md"))
    assert dual_workspace.file_read(report) == "Sourced findings"


@pytest.mark.asyncio
async def test_worker_code_runs_in_its_output_directory_with_the_parent_execution_session(
    workspace, monkeypatch
):
    from core.plugins import execute_code as execution
    from core.plugins.sandbox import local
    from core.sub_agent import AgentTypeConfig, SubAgentRunner

    monkeypatch.setattr(local, "WORKSPACE_ROOT", str(workspace._backend.root))
    monkeypatch.setattr(execution, "_BACKEND", local.LocalSubprocessBackend())
    runner = SubAgentRunner(
        AgentTypeConfig(name="worker", execution_mode="worker", required_tools=["execute_code"]),
        "alice",
        "child-history",
        workspace=workspace,
        internal_worker=True,
        output_dir="workers/task/attempt",
        sandbox_session_id="parent",
        inherited_plugins=["ExecuteCodeTool"],
    )
    code_tools = [tool for tool in runner._build_tools() if tool.name == "execute_code"]
    assert len(code_tools) == 1
    tool = code_tools[0]
    assert runner.session_id != "parent" and tool._session_id == "parent"
    result = json.loads(
        await tool(
            execution.ExecuteCodeParams(
                language="python", code='from pathlib import Path; Path("computed.txt").write_text("42")'
            )
        )
    )
    assert result["success"] and workspace.file_read("workers/task/attempt/computed.txt") == "42"


def test_worker_tools_can_be_disabled_without_disabling_interactive_agents(workspace, monkeypatch):
    import core.agent as main

    monkeypatch.setattr(main, "WORKER_ENABLED", False)
    parent = EvoAgent("alice", "main", plugins=[], workspace=workspace)
    names = {tool.name for tool in parent.tools}
    assert "worker" not in names and "worker_status" not in names
    assert {"sub_agent", "workspace_file"} <= names


@pytest.mark.asyncio
async def test_per_turn_limit_is_enforced_and_rejected_work_is_not_started(workspace, monkeypatch):
    monkeypatch.setattr(settings, "WORKER_MAX_PER_TURN", 1)
    registry = WorkerRegistry(workspace, "main")
    gate = asyncio.Event()

    async def run(job):
        await gate.wait()

    registry.launch("a", "A", "workers/a/run", run, timeout=1)
    with pytest.raises(ValueError, match="limit"):
        registry.launch("b", "B", "workers/b/run", run, timeout=1)
    assert "b" not in registry.jobs
    gate.set()
    await registry.jobs["a"].task


@pytest.mark.asyncio
async def test_no_output_is_not_reported_as_completed(registry):
    async def run(job):
        return "completed"

    job = registry.launch("empty", "Empty", "workers/empty/run", run, timeout=1)
    await job.task
    assert job.receipt["status"] == "failed" and not job.receipt["files"]


@pytest.mark.asyncio
async def test_cleanup_records_terminal_state_when_compression_retired_the_dispatch(registry):
    from utils.history_compressor import HistoryCompressor

    async def run(job):
        await asyncio.Event().wait()

    job = registry.launch("research", "Research", "workers/research/run", run, timeout=1)
    history = [
        {
            "role": "user",
            "content": "<summary>Work running</summary>",
            "delegation_receipts": [
                {
                    "type": "worker_receipt",
                    "owner": "worker",
                    "task_id": "research",
                    "status": "running",
                }
            ],
        }
    ]
    await registry.close()
    registry.finalize_history(history)
    assert job.finished
    receipts = HistoryCompressor.delegation_receipts(history)
    assert len(receipts) == 1 and receipts[-1]["status"] == "cancelled"
    assert receipts[-1]["owner"] == "worker" and receipts[-1]["output_dir"] == job.output_dir
    assert history[-2]["tool_calls"][0]["id"] == history[-1]["tool_call_id"]


@pytest.mark.asyncio
async def test_a_parent_question_stops_unfinished_workers_without_reentering(workspace, monkeypatch):
    def script(turn, tools, input):
        if turn == 1:
            return [call("worker", {"task": "Research", "task_id": "research"})]
        assert turn == 2
        return [
            call(
                "ask_user_question", {"question": "Which sources?", "options": [{"label": "Provided files"}]}
            )
        ]

    parent = parent_for(workspace, monkeypatch, ScriptedModel(script))
    tool = next(tool for tool in parent.tools if tool.name == "worker")

    async def execute(job, *args):
        await asyncio.Event().wait()

    monkeypatch.setattr(tool, "_execute", execute)
    events = [event async for event in parent.run_stream("Research")]
    assert any(event["code"] == 10 for event in events)
    assert parent._workers.jobs["research"].receipt["status"] == "cancelled"
    assert parent.model.turn == 2
    progress = [json.loads(event["content"]) for event in events if event["code"] == 5]
    progress = [event for event in progress if event.get("name") == "worker_progress"]
    assert json.loads(progress[-1]["result"])["status"] == "cancelled"
    assert progress[-1]["streaming"] is False


@pytest.mark.parametrize("arguments", ["[]", "null", "1", '"text"', "{partial"])
def test_malformed_delegation_arguments_do_not_break_interruption_snapshot(workspace, registry, arguments):
    from core.plugins.internal import SubAgentTool

    for tool in (SubAgentTool(workspace=workspace), WorkerTool("alice", "main", workspace, registry)):
        assert tool.interruption_snapshot(arguments) == []


@pytest.mark.asyncio
async def test_http_disconnect_stops_the_worker_and_saves_both_histories(workspace, monkeypatch):
    from fastapi import FastAPI

    import app.api.routers.agent as router
    import core.agent as main
    import core.sub_agent as sub

    child_pause, parent_pause = PausedTool(), PausedTool()

    def script(turn, tools, input):
        return (
            [call("worker", {"task": "Research", "task_id": "research"})]
            if turn == 1
            else [call("paused", {})]
        )

    parent = parent_for(workspace, monkeypatch, ScriptedModel(script))
    parent.tools.append(parent_pause)
    monkeypatch.setattr(sub.SubAgentRunner, "_build_tools", lambda self: [child_pause])
    monkeypatch.setattr(
        sub, "_SDKAgent", lambda **kwargs: main._SDKAgent(**(kwargs | {"model": model_for_calls()}))
    )
    monkeypatch.setattr(router, "_create_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(router, "EvoAgent", lambda **kwargs: parent)
    monkeypatch.setattr(router, "INPUT_EXTRACTORS", {})
    monkeypatch.setattr(router, "OUTPUT_EXTRACTORS", {})
    app = FastAPI()
    app.include_router(router.router)
    body = json.dumps({"userId": "alice", "sessionId": "main", "data": {"input": "Research"}}).encode()
    disconnected, delivered = asyncio.Event(), False

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
        "path": "/chat",
        "raw_path": b"/chat",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(child_pause.started.wait(), 2)
    await asyncio.wait_for(parent_pause.started.wait(), 2)
    disconnected.set()
    await asyncio.wait_for(task, 3)
    job = parent._workers.jobs["research"]
    assert job.finished and job.receipt["status"] == "cancelled"
    assert workspace.history_load("main")["interrupted"]
    assert workspace.history_load(job.session_id)["interrupted"]


@pytest.mark.asyncio
async def test_an_sdk_run_can_be_closed_before_its_event_stream_is_entered():
    from agents import Runner

    from core.context_compaction import CompactingAgent
    from core.runtime.execution import SDKRunSession

    model = ScriptedModel(lambda *args: [final_message("Must not run")])
    agent = CompactingAgent(name="unentered", instructions="Offline", model=model)
    session = SDKRunSession(agent, SimpleNamespace(should_compress=lambda history: False), [], [])
    session.start(Runner.run_streamed, agent, input="Task", max_turns=2)
    session.close()
    await asyncio.sleep(0)
    assert session.result.is_complete and session.interrupted and model.turn == 0


@pytest.mark.asyncio
async def test_cancelled_retry_cannot_rewrite_a_previous_attempts_receipt(registry):
    async def run(job):
        await asyncio.Event().wait()

    registry.launch("same", "Retry", "workers/task/current", run, timeout=1)
    previous = {"task_id": "same", "status": "running", "output_dir": "workers/task/previous"}
    current = {"task_id": "same", "status": "running", "output_dir": "workers/task/current"}
    history = [
        {"role": "tool", "name": "worker", "content": json.dumps(payload)} for payload in (previous, current)
    ]
    original = history[0]["content"]
    await registry.close()
    registry.finalize_history(history)
    assert history[0]["content"] == original
    assert json.loads(history[1]["content"])["status"] == "cancelled"
