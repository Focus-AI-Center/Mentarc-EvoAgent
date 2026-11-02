"""Keep original skill guides and their dynamically granted tools together."""

from core.agent_compat import convert_tools
from core.plugins import _normalize_plugin_name, initialize_plugins
from core.skills import skill_from_md
from utils.skill_history import loaded_skill_names
from utils.tool_names import canonical_tool_name
from utils.ws_call import ws_call


class SkillToolMounter:
    def __init__(self, tools, *, user_id, session_id, workspace):
        self.tools = tools
        self.kwargs = dict(user_id=user_id, session_id=session_id, workspace=workspace)
        self.workspace = workspace
        self.loaded = set()
        self.required = set()
        self.owned = []
        self.agent = None
        self.catalog = None
        for tool in tools:
            if tool.name == "skill":
                tool._on_loaded = self.on_loaded

    async def on_loaded(self, name, raw=None):
        if raw is None:
            raw = await ws_call(self.workspace, "skill_get", name)
        if not raw:
            return
        skill = skill_from_md(raw, fallback_name=name)
        names = {canonical_tool_name(tool) for tool in skill.required_tools}
        existing = {_normalize_plugin_name(tool.name) for tool in self.tools}
        missing = [
            name
            for name in names
            if not name.startswith("mcp__") and _normalize_plugin_name(name) not in existing
        ]
        new = initialize_plugins(sorted(missing), **self.kwargs)
        self.tools.extend(new)
        self.owned.extend(new)
        self.required.update(names)
        self.loaded.add(name)
        if self.agent:
            if self.catalog:
                await self.catalog.mount_skill_tools(names, convert_tools(new))
            else:
                self.agent.tools.extend(convert_tools(new))

    async def restore(self, history):
        for name in loaded_skill_names(history):
            await self.on_loaded(name)
        for tool in self.tools:
            if tool.name == "skill":
                tool._got_skills.update(self.loaded)

    async def bind(self, agent, catalog):
        self.agent, self.catalog = agent, catalog
        if catalog:
            await catalog.mount_skill_tools(self.required)

    def on_context_compacted(self):
        owned_ids = {id(tool) for tool in self.owned}
        owned_names = {tool.name for tool in self.owned}
        self.tools[:] = [tool for tool in self.tools if id(tool) not in owned_ids]
        if self.catalog:
            self.catalog.unmount_skill_tools(owned_names)
        elif self.agent:
            self.agent.tools[:] = [tool for tool in self.agent.tools if tool.name not in owned_names]
        self.owned.clear()
        self.required.clear()
        self.loaded.clear()
        for tool in self.tools:
            if tool.name == "skill":
                tool.on_context_compacted()
