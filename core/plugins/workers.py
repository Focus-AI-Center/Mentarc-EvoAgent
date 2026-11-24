"""Internal file-producing workers and request-local result collection."""

import json
from copy import deepcopy

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.agent_registry import agent_catalog
from core.delegation import TASK_ID_PATTERN, new_task_id, task_session_base
from core.runtime.delegation_context import render_delegation_context
from utils.tool_results import tool_observation
from utils.usage import current_usage


class WorkerParams(BaseModel):
    task: str = Field(
        min_length=1,
        max_length=30000,
        description="Self-contained goal, inputs, output requirements and boundaries",
    )
    title: str = Field(default="Worker task", min_length=1, max_length=120)
    agent_type: str = Field(
        default="worker",
        pattern=TASK_ID_PATTERN,
        description="A worker-mode agent profile from the available catalog",
    )
    skills: list[str] = Field(
        default_factory=list, max_length=20, description="Skills to load before executing the task"
    )
    task_id: str | None = Field(
        default=None,
        pattern=TASK_ID_PATTERN,
        description="Reuse a completed task ID to continue its saved history",
    )
    background: bool = Field(
        default=True, description="Return running immediately; false waits briefly before returning"
    )


class WorkerStatusParams(BaseModel):
    task_ids: list[str] = Field(
        default_factory=list, description="Task IDs to collect; empty selects all tasks in this request"
    )
    wait_seconds: float = Field(default=30, ge=0, le=60)


class WorkerTool(BaseTool):
    name = "worker"
    description = (
        "Run an independent task using skills and return workspace files to you. "
        "Use for substantial independent parallel work or isolating large reads; do simple work directly. "
        "The worker cannot ask the user, update long-term state, propose skills or deliver user-facing output. "
        "Supply complete inputs and shared decisions. With background=true it returns running immediately; "
        "continue your own work, then collect with worker_status. Read and verify returned files before delivery."
    )
    param_anno = WorkerParams

    def __init__(self, user_id, session_id, workspace, registry, *, context_fn=None, plugins_fn=None):
        self.user_id, self.session_id = user_id, session_id
        self.workspace, self.registry = workspace, registry
        self.context_fn, self.catalog = context_fn, None
        self.plugins_fn = plugins_fn

    def set_catalog(self, catalog):
        self.catalog = catalog
        profiles = [f"- {c.name}: {c.description}" for c in catalog.values() if c.execution_mode == "worker"]
        self.description = type(self).description + "\nAvailable worker profiles:\n" + "\n".join(profiles)

    async def __call__(self, params):
        try:
            if not params.task.strip():
                raise ValueError("A worker task cannot be blank.")
            if self.catalog is None:
                self.set_catalog(await ws_call_catalog(self.workspace))
            config = self.catalog.get(params.agent_type)
            if config is None or config.execution_mode != "worker":
                raise ValueError("Choose an available worker-mode agent profile.")
            task_id = params.task_id or new_task_id()
            base = task_session_base(self.session_id, task_id)
            from uuid import uuid4

            output_dir = f"workers/{base}/{uuid4().hex[:12]}"
            # Do not capture the parent's mutable history or model-authored instructions.
            source = deepcopy(self.context_fn() if self.context_fn else {})
            usage = current_usage()
            inherited_plugins = tuple(self.plugins_fn() if self.plugins_fn else ())

            async def run(job):
                return await self._execute(
                    job, params, config.model_copy(deep=True), base, source, usage, inherited_plugins
                )

            job = self.registry.launch(
                task_id, params.title, output_dir, run, timeout=min(config.timeout, 300), owner=config.name
            )
            job.request = params.model_dump()
            if not params.background:
                from configs.settings import WORKER_WAIT_SECONDS

                result = await self.registry.collect([task_id], WORKER_WAIT_SECONDS)
                if result["finished"]:
                    return json.dumps(result["finished"][0], ensure_ascii=False)
            return json.dumps(
                {
                    "type": "worker_receipt",
                    "owner": config.name,
                    "task_id": task_id,
                    "title": params.title,
                    "status": "running",
                    "output_dir": output_dir,
                    "note": "Continue independent work, then use worker_status to collect the result.",
                }
            )
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})

    async def _execute(self, job, params, config, base, source, usage, inherited_plugins):
        from contextlib import aclosing

        from core.sub_agent import SubAgentRunner

        runner = SubAgentRunner(
            agent_type=params.agent_type,
            agent_config=config,
            user_id=self.user_id,
            session_id=base,
            workspace=self.workspace,
            usage_collector=usage,
            internal_worker=True,
            output_dir=job.output_dir,
            preload_skills=params.skills,
            sandbox_session_id=self.session_id,
            inherited_plugins=inherited_plugins,
        )
        job.session_id = runner.session_id
        task = params.task + f"\n\nWrite task outputs under `{job.output_dir}/`."
        async with aclosing(runner.run_stream(task, render_delegation_context(source, ""))) as events:
            async for event in events:
                if event.get("code") == 2:
                    job.text.append(str(event.get("content", "")))
                elif event.get("code") in (4, 5):
                    try:
                        data = json.loads(event.get("content") or "{}")
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if event["code"] == 4:
                        job.progress = "Using " + str(data.get("name", "tool"))
                        job.tool_calls.append(
                            {"tool": data.get("name"), "call_id": data.get("id"), "status": "running"}
                        )
                        job.tool_calls[:] = job.tool_calls[-100:]
                        self.registry.progress(job)
                    else:
                        outcome = tool_observation(data.get("result"))
                        job.tool_outcomes.append(
                            {
                                "tool": data.get("name"),
                                "call_id": data.get("id"),
                                "status": data.get("status", outcome["status"]),
                                "is_error": data.get("is_error", outcome["is_error"]),
                            }
                        )
                        for call in reversed(job.tool_calls):
                            if call["call_id"] == data.get("id"):
                                call["status"] = (
                                    "failed" if job.tool_outcomes[-1]["is_error"] else "completed"
                                )
                                self.registry.progress(job)
                                break
                elif event.get("code") in (0, 10, 12):
                    raise ValueError("An internal worker produced an invalid interaction event.")
        return getattr(runner, "run_status", "failed" if getattr(runner, "failure", None) else "completed")

    def interruption_snapshot(self, arguments):
        try:
            request = json.loads(arguments)
        except (ValueError, TypeError):
            return []
        if not isinstance(request, dict):
            return []
        return [
            job.receipt or {"task_id": job.task_id, "status": job.state}
            for job in self.registry.jobs.values()
            if all(
                job.request.get(key) == request.get(key, "worker" if key == "agent_type" else None)
                for key in ("task", "agent_type", "task_id")
            )
        ]


async def ws_call_catalog(workspace):
    import asyncio

    return await asyncio.to_thread(agent_catalog, workspace)


class WorkerStatusTool(BaseTool):
    name = "worker_status"
    description = "Collect completed worker receipts and file paths. Wait for up to 60 seconds if none are ready; waiting does not cancel tasks. Task IDs belong to this request."
    param_anno = WorkerStatusParams

    def __init__(self, registry):
        self.registry = registry

    async def __call__(self, params):
        try:
            return json.dumps(
                await self.registry.collect(params.task_ids, params.wait_seconds), ensure_ascii=False
            )
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})
