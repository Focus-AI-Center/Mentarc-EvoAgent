"""One tool policy for initial schemas, skill mounts and deferred discovery."""

import json
from copy import deepcopy
from dataclasses import dataclass, field, replace

from agents import FunctionTool

from utils.tool_names import canonical_tool_name


@dataclass(frozen=True)
class ToolPolicy:
    denied: frozenset[str] = frozenset()
    actions: dict[str, frozenset[str]] = field(default_factory=dict)

    def allows(self, name: str) -> bool:
        return canonical_tool_name(name) not in self.denied

    def filter(self, tools):
        result = []
        for tool in tools:
            if not self.allows(tool.name):
                continue
            allowed = self.actions.get(canonical_tool_name(tool.name))
            if not allowed or not isinstance(tool, FunctionTool):
                result.append(tool)
                continue
            if getattr(tool.on_invoke_tool, "_tool_policy", None) is self:
                result.append(tool)
                continue
            result.append(self._guard(tool, allowed))
        return result

    def _guard(self, tool, allowed):
        invoke = tool.on_invoke_tool

        async def guarded(ctx, arguments):
            try:
                params = json.loads(arguments)
            except (ValueError, TypeError):
                params = {}
            default = tool.params_json_schema.get("properties", {}).get("action", {}).get("default")
            if not isinstance(params, dict) or params.get("action", default) not in allowed:
                return json.dumps(
                    {"status": "error", "error": "This action is unavailable to this delegated agent."}
                )
            return await invoke(ctx, arguments)

        guarded._tool_policy = self
        schema = deepcopy(tool.params_json_schema)
        action = schema.get("properties", {}).get("action")
        if isinstance(action, dict):
            action["enum"] = sorted(allowed)
        return replace(
            tool,
            on_invoke_tool=guarded,
            params_json_schema=schema,
            description=tool.description + " Allowed delegated actions: " + ", ".join(sorted(allowed)) + ".",
        )


DELEGATED_POLICY = ToolPolicy(
    denied=frozenset({"ask_user_question", "deliver_artifacts"}),
    actions={"skill": frozenset({"list", "get", "get_ref"})},
)


WORKER_POLICY = ToolPolicy(
    denied=frozenset(
        {
            "ask_user_question",
            "update_user_profile",
            "schedule_manager",
            "sub_agent",
            "worker",
            "worker_status",
            "parent_history",
            "deliver_artifacts",
            "html_render",
        }
    ),
    actions={
        "skill": frozenset({"list", "get", "get_ref"}),
        "memory": frozenset({"read", "get"}),
    },
)
