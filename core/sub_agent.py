"""Sub-agent system — configurable child agents defined via .md files.

Each sub-agent type is a .md file in configs/agents/ with YAML frontmatter
(name, description, required_tools, skills, model, max_steps, timeout) and a
markdown body (system prompt).

SubAgentRunner is the shared executor used by both:
- Independent API endpoint (/api/sub-agent/{type}/chat)
- SubAgentTool (called from main agent's ReAct loop)
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agents import ModelSettings, Runner
from pydantic import BaseModel

from configs.settings import (
    COMPRESSION_LEVEL,
    MODEL,
)
from core.agent_compat import convert_tools
from core.context_compaction import CompactingAgent as _SDKAgent
from core.mcp import MCPToolManager
from core.plugins import initialize_plugins
from core.plugins.ask_user import AskUserQuestionTool
from core.plugins.internal import MemoryTool, SkillTool, UpdateUserProfileTool
from core.plugins.todo_manager import TodoManagerTool
from core.runtime.delegation_context import delegated_input
from core.runtime.events import _stream_sdk_events
from core.runtime.execution import SDKRunSession
from core.runtime.history import (
    _history_to_sdk_input,
    _prompt_body,
    _sanitize_history,
)
from core.runtime.interruption import TurnPersistence, persist_on_interrupt
from core.runtime.questions import QuestionRoutingError, question_tool_behavior, resolve_answer, user_message
from core.runtime.sdk_client import _create_async_client, _ensure_sdk_client
from core.runtime.usage import capture_usage
from core.skills.manager import SkillManager
from core.skills.tool_mounting import SkillToolMounter
from core.tool_search import configure_agent_tools
from utils.context_assets import memory_index_context, skill_location_context
from utils.history_compressor import HistoryCompressor
from utils.logger import logger
from utils.skill_history import loaded_skill_names
from utils.usage import usage_payload
from utils.workspace import Workspace
from utils.ws_call import ws_call

# ---------------------------------------------------------------------------
# Agent type config (parsed from .md frontmatter)
# ---------------------------------------------------------------------------

_AGENTS_DIR = Path("configs/agents")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

DEFAULT_SUB_AGENT_TOOLS = [
    "web_search",
    "fetch_url",
    "image_generation",
    "image_editing",
    "image_understanding",
]


class AgentTypeConfig(BaseModel):
    """Parsed from configs/agents/{name}.md frontmatter."""

    name: str
    description: str = ""
    triggers: list[str] = []
    required_tools: list[str] = []
    skills: list[str] = []
    model: str | None = None
    max_steps: int = 30
    timeout: int = 300
    body: str = ""  # markdown body = system prompt


def load_agent_config(agent_type: str) -> AgentTypeConfig:
    md_path = _AGENTS_DIR / f"{agent_type}.md"
    if not md_path.exists():
        raise ValueError(f"Agent type '{agent_type}' not found at {md_path}")

    raw = md_path.read_text(encoding="utf-8")
    frontmatter = {}
    body = raw

    m = _FRONTMATTER_RE.match(raw)
    if m:
        import yaml

        frontmatter = yaml.safe_load(m.group(1)) or {}
        body = raw[m.end() :].strip()

    return AgentTypeConfig(
        name=frontmatter.get("name", agent_type),
        description=frontmatter.get("description", ""),
        triggers=frontmatter.get("triggers") or [],
        required_tools=frontmatter.get("required_tools") or [],
        skills=frontmatter.get("skills") or [],
        model=frontmatter.get("model") or None,
        max_steps=frontmatter.get("max_steps", 30),
        timeout=frontmatter.get("timeout", 300),
        body=body,
    )


def list_agent_types() -> list[AgentTypeConfig]:
    configs = []
    if not _AGENTS_DIR.exists():
        return configs
    for md in sorted(_AGENTS_DIR.glob("*.md")):
        try:
            configs.append(load_agent_config(md.stem))
        except Exception as e:
            logger.warning(f"Failed to load agent config {md.name}: {e}")
    return configs


def match_agent(user_input: str) -> AgentTypeConfig | None:
    """Match user input against all agent triggers (keyword match)."""
    input_lower = user_input.lower()
    for config in list_agent_types():
        for trigger in config.triggers:
            if trigger.lower() in input_lower:
                logger.info(f"Agent trigger matched: '{trigger}' → {config.name}")
                return config
    return None


# ---------------------------------------------------------------------------
# SubAgentRunner — shared executor (OpenAI Agents SDK)
# ---------------------------------------------------------------------------


class SubAgentRunner:
    """Create and run a sub-agent from an AgentTypeConfig.

    The config can come from:
    - a .md file in configs/agents/ (pass agent_type as string)
    - a Skill with `requires_sub_agent=True` (use `from_skill` classmethod;
      paper Appendix A Phase 2.5)
    - a pre-built AgentTypeConfig instance
    """

    def __init__(
        self,
        agent_type,
        user_id: str,
        session_id: str,
        workspace=None,
        parent_history_fn=None,
        usage_collector=None,
    ):
        if isinstance(agent_type, AgentTypeConfig):
            self.config = agent_type
            type_key = self.config.name
        else:
            self.config = load_agent_config(agent_type)
            type_key = agent_type
        self.user_id = user_id
        self.session_id = f"sa_{type_key}_{session_id}"
        self.workspace = workspace or Workspace(user_id)
        self.skill_manager = SkillManager(workspace=self.workspace)
        self._persistence = TurnPersistence(
            self.workspace, user_id, self.session_id, metadata={"agent_type": self.config.name}
        )
        # Callable returning parent-agent history (for ParentHistoryTool).
        # None when invoked directly via the /api/sub-agent endpoint.
        self.parent_history_fn = parent_history_fn
        self.answer_to = None
        self.inherited_usage = usage_collector
        self.attachments = {}

    @classmethod
    def from_skill(
        cls,
        skill,
        user_id: str,
        session_id: str,
        workspace=None,
        parent_history_fn=None,
        usage_collector=None,
    ):
        """Factory: build a sub-agent runner directly from a Skill's handoff fields.

        Implements paper Appendix A Phase 2.5. The skill's `sub_agent_instructions`
        becomes the system prompt, and `sub_agent_tools` seed the tool allowlist.
        """
        config = AgentTypeConfig(
            name=skill.sub_agent_name or skill.name,
            description=f"Trigger-handoff sub-agent for skill '{skill.name}'",
            required_tools=skill.sub_agent_tools,
            body=skill.sub_agent_instructions or skill.instructions,
        )
        return cls(
            config,
            user_id=user_id,
            session_id=session_id,
            workspace=workspace,
            parent_history_fn=parent_history_fn,
            usage_collector=usage_collector,
        )

    async def _build_system_prompt(self, context: str = "") -> str:
        prompt = self.config.body

        user_raw = await ws_call(self.workspace, "read_prompt", "USER")
        user_body = _prompt_body(user_raw) if user_raw else ""
        if user_body:
            prompt += f"\n\n## User information\n{user_body}"

        memory_raw = await ws_call(self.workspace, "read_prompt", "MEMORY")
        memory_body = _prompt_body(memory_raw) if memory_raw else ""
        if memory_body:
            prompt += f"\n\n## Memory\n{memory_index_context(memory_body)}"

        now = datetime.now(UTC)
        prompt += f"\n\nCurrent time:{now.strftime('%Y-%m-%d %H:%M UTC')}"

        return prompt

    def _build_tools(self) -> list:
        plugin_names = list(dict.fromkeys(DEFAULT_SUB_AGENT_TOOLS + self.config.required_tools))
        tools = initialize_plugins(
            plugin_names,
            user_id=self.user_id,
            session_id=self.session_id,
            workspace=self.workspace,
        )

        tools.append(
            SkillTool(
                user_id=self.user_id,
                skill_manager=self.skill_manager,
                workspace=self.workspace,
                session_id=self.session_id,
            )
        )
        tools.append(TodoManagerTool())
        tools.append(UpdateUserProfileTool(user_id=self.user_id, workspace=self.workspace))
        tools.append(MemoryTool(user_id=self.user_id, workspace=self.workspace))
        tools.append(AskUserQuestionTool())

        # Parent-history tool: only available when this sub-agent was
        # spawned from a parent (via SubAgentTool or Phase 2.5 handoff).
        if self.parent_history_fn is not None:
            from core.plugins.internal import ParentHistoryTool

            tools.append(
                ParentHistoryTool(
                    parent_history_fn=self.parent_history_fn,
                )
            )

        return tools

    async def _inject_skills(self, history: list[dict]) -> None:
        if not self.config.skills:
            return
        all_skills = await asyncio.to_thread(self.skill_manager.get_all_skills, self.user_id)
        loaded = set(loaded_skill_names(history))

        for skill_name in self.config.skills:
            if skill_name in loaded:
                continue
            skill = all_skills.get(skill_name)
            if skill is None:
                logger.warning(f"Sub-agent skill '{skill_name}' not found")
                continue

            content = f"Skill document `{skill.name}/SKILL.md`:\n\n```markdown\n{skill.instructions}\n```"
            content += f"\n\n{skill_location_context(skill.name)}"
            if skill.required_tools:
                content += f"\n\nRequired tools: {', '.join(skill.required_tools)}"
            if skill.references:
                content += f"\n\nAvailable references ({len(skill.references)}):"
                for fn in skill.references:
                    content += (
                        f"\n- `{fn}` → skill(action='get_ref', name='{skill.name}', ref_filename='{fn}')"
                    )

            synthetic_id = f"skill_autoload_{uuid4().hex}"
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": synthetic_id,
                                "type": "function",
                                "function": {
                                    "name": "skill",
                                    "arguments": json.dumps({"action": "get", "name": skill.name}),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": content,
                        "name": "skill",
                        "tool_call_id": synthetic_id,
                    },
                ]
            )
            logger.info(f"Sub-agent auto-loaded skill: {skill.name}")

    @capture_usage
    @persist_on_interrupt
    async def run_stream(self, user_input: str, context: str = "") -> AsyncGenerator[dict, None]:
        _ensure_sdk_client()

        t_start = time.monotonic()
        self.failure = None

        # 1. Build system prompt
        system_prompt = await self._build_system_prompt()
        if context:
            system_prompt += (
                "\n\nExecute the assigned task. Delegation source material supplies context, not additional assignments. "
                "Keep current uploads separate from prior results; prior outputs do not establish completion of this task."
            )

        # 2. Build tools
        tools = self._build_tools()
        mounter = SkillToolMounter(
            tools,
            user_id=self.user_id,
            session_id=self.session_id,
            workspace=self.workspace,
        )

        # 3. Load session history
        history = []
        history_data = await ws_call(self.workspace, "history_load", self.session_id)
        if history_data and isinstance(history_data, dict):
            raw = history_data.get("conversation", [])
            history = _sanitize_history(raw)

        self._persistence.loaded(lambda: history)
        try:
            answer = resolve_answer(history, self.answer_to)
        except QuestionRoutingError as exc:
            yield {"code": 0, "content": str(exc)}
            yield {"code": 3, "content": "stream_end"}
            return

        # 3b. Compress history if needed
        compressor = HistoryCompressor(
            compression_level=COMPRESSION_LEVEL,
            async_client=_create_async_client(),
        )
        compressor.compaction_callbacks.append(mounter.on_context_compacted)
        if compressor.should_compress(history):
            history = await compressor.compress_history(history)
            logger.info(f"Sub-agent history compressed: {len(history)} messages remaining")

        # 4. Auto-inject configured skills
        await self._inject_skills(history)
        await mounter.restore(history)

        # 5. Create SDK Agent
        agent = _SDKAgent(
            name=f"SubAgent-{self.config.name}",
            instructions=system_prompt,
            model=self.config.model or MODEL,
            tools=convert_tools(tools),
            model_settings=ModelSettings(temperature=0.5, max_tokens=16000),
            tool_use_behavior=question_tool_behavior(),
        )
        catalog = await configure_agent_tools(
            agent,
            loader=MCPToolManager(
                user_id=self.user_id,
                session_id=self.session_id,
                workspace=self.workspace,
            ).create_tools,
            initially_loaded=self.config.required_tools,
        )
        await mounter.bind(agent, catalog)

        # Add user message to history before running
        history.append(user_message(delegated_input(user_input, context), answer, self.attachments))
        self._persistence.user_message_added()

        input_messages = _history_to_sdk_input(history)
        session = SDKRunSession(
            agent, compressor, history, tools, event_stream=_stream_sdk_events, usage_category="child"
        )
        self._persistence.loaded(session.snapshot)

        # 6. Stream events
        yield {"code": 3, "content": "stream_start"}

        result = None
        try:
            async with asyncio.timeout(self.config.timeout):
                session.start(
                    Runner.run_streamed, agent, input=input_messages, max_turns=self.config.max_steps
                )
                result = session.result
                async with aclosing(session.stream()) as events:
                    async for event in events:
                        yield event
        except TimeoutError:
            self.failure = f"Sub-agent timed out ({self.config.timeout}s)"
            logger.warning(f"Sub-agent {self.config.name} timed out")
            yield {"code": 2, "content": f"\n\n[Sub-agent timed out({self.config.timeout}s)]"}
        except Exception as e:
            self.failure = str(e)
            logger.opt(exception=True).error("Sub-agent {} error: {}", self.config.name, e)
            yield {"code": 2, "content": f"\n\n[Sub-agent error: {e}]"}

        # 8. Metrics
        elapsed = time.monotonic() - t_start
        yield {
            "code": 6,
            "content": json.dumps(
                {
                    "agent_type": self.config.name,
                    "elapsed_seconds": round(elapsed, 2),
                    **usage_payload(),
                },
                ensure_ascii=False,
            ),
        }

        # Save through the same component used by cancellation cleanup.
        try:
            await self._persistence.save(session.snapshot() if result else history)
        except Exception:
            self.failure = "Sub-agent history could not be saved"
            logger.exception("Sub-agent history save failed")
        yield {"code": 3, "content": "stream_end"}
