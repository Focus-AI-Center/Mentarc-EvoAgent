"""TODO Manager plugin — task progress tracking for complex multi-step tasks.

Stateful tool that maintains an in-memory task list. The agent uses `add` to
create a task plan and `update` to mark individual steps as they progress.
Each call returns a full snapshot that agent.py emits as SSE code 9 for
frontend rendering.

Supports cross-turn resume: agent.py injects `_history_tasks` from session
history. When `update` is called on an empty `_tasks` and there are incomplete
history tasks, the list is lazily restored so the agent can continue where it
left off.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from core.agent_compat import BaseTool

_VALID_STATUSES = {"pending", "in_progress", "completed"}


class TodoManagerParams(BaseModel):
    action: Literal["add", "update"] = Field(
        description="Action: add initializes tasks; update changes one task status"
    )
    # Used by add
    tasks: list[dict] | None = Field(
        default=None,
        description=(
            "Complete task list for add. Each item: {id: str, content: str, status: pending/in_progress/completed}"
        ),
    )
    # Used by update
    id: str | int | None = Field(
        default=None,
        description="Task ID for update",
    )

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id_to_str(cls, v):
        return str(v) if v is not None else v

    status: str | None = Field(
        default=None,
        description="New status for update: pending/in_progress/completed",
    )


class TodoManagerTool(BaseTool):
    name: str = "todo_manager"
    description: str = (
        "Manage task progress shown in the interface.\n"
        "- add: Initialize the task list (provide a tasks array)\n"
        "- update: Update one task (provide id and status)"
    )
    param_anno = TodoManagerParams

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tasks: list[dict] = []
        self._history_tasks: list[dict] = []  # injected by agent.py from session history

    async def __call__(self, params: TodoManagerParams) -> str:
        if params.action == "add":
            if not params.tasks:
                return json.dumps({"error": "add requires a tasks list"}, ensure_ascii=False)
            self._tasks = []
            for t in params.tasks:
                status = t.get("status", "pending")
                if status not in _VALID_STATUSES:
                    status = "pending"
                self._tasks.append(
                    {
                        "id": str(t.get("id", "")),
                        "content": str(t.get("content", "")),
                        "status": status,
                    }
                )

        elif params.action == "update":
            if not params.id:
                return json.dumps({"error": "update action requiresprovide id"}, ensure_ascii=False)
            status = params.status or "completed"
            if status not in _VALID_STATUSES:
                return json.dumps(
                    {"error": f"Invalid status: {status},optional: pending/in_progress/completed"},
                    ensure_ascii=False,
                )
            # Lazy restore: if _tasks is empty, try to recover from history
            if not self._tasks and self._history_tasks:
                has_incomplete = any(t.get("status") != "completed" for t in self._history_tasks)
                if has_incomplete:
                    self._tasks = [t.copy() for t in self._history_tasks]
            found = False
            target_id = str(params.id)
            for t in self._tasks:
                if t["id"] == target_id:
                    t["status"] = status
                    found = True
                    break
            if not found:
                # Task list may have been reset between turns; return empty
                # snapshot instead of error so agent can continue.
                return json.dumps({"tasks": self._tasks}, ensure_ascii=False)

        return json.dumps({"tasks": self._tasks}, ensure_ascii=False)
