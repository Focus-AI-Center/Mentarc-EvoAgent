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
from datetime import UTC, datetime
from pathlib import Path

import httpx
from agents import Agent as _SDKAgent
from agents import ModelSettings, Runner
from openai import AsyncOpenAI
from pydantic import BaseModel

from configs.settings import (
    COMPRESSION_LEVEL,
    COMPRESSION_MODEL,
    MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from core.agent_compat import convert_tools
from core.mcp import MCPToolManager
from core.plugins import initialize_plugins
from core.plugins.ask_user import AskUserQuestionTool
from core.plugins.internal import MemoryTool, SkillTool, UpdateUserProfileTool
from core.plugins.todo_manager import TodoManagerTool
from core.skills.manager import SkillManager
from core.tool_search import configure_agent_tools
from utils.context_assets import memory_index_context, skill_location_context
from utils.history_compressor import HistoryCompressor
from utils.logger import logger
from utils.workspace import Workspace
from utils.ws_call import ws_call

# Lazy imports from agent.py to avoid circular dependency
_prompt_body = None
_sanitize_history = None
_stream_sdk_events = None
_history_to_sdk_input = None
_sdk_result_to_history = None
_ensure_sdk_client = None


def _ensure_imports():
    global _prompt_body, _sanitize_history, _stream_sdk_events
    global _history_to_sdk_input, _sdk_result_to_history, _ensure_sdk_client
    if _prompt_body is None:
        from core.agent import (
            _ensure_sdk_client as _esc,
        )
        from core.agent import (
            _history_to_sdk_input as _htsi,
        )
        from core.agent import (
            _prompt_body as _pb,
        )
        from core.agent import (
            _sanitize_history as _sh,
        )
        from core.agent import (
            _sdk_result_to_history as _srth,
        )
        from core.agent import (
            _stream_sdk_events as _sse,
        )

        _prompt_body = _pb
        _sanitize_history = _sh
        _stream_sdk_events = _sse
        _history_to_sdk_input = _htsi
        _sdk_result_to_history = _srth
        _ensure_sdk_client = _esc


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

    def __init__(self, agent_type, user_id: str, session_id: str, workspace=None, parent_history_fn=None):
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
        # Callable returning parent-agent history (for ParentHistoryTool).
        # None when invoked directly via the /api/sub-agent endpoint.
        self.parent_history_fn = parent_history_fn

    @classmethod
    def from_skill(cls, skill, user_id: str, session_id: str, workspace=None, parent_history_fn=None):
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
        )

    async def _build_system_prompt(self, context: str = "") -> str:
        _ensure_imports()
        prompt = self.config.body

        user_raw = await ws_call(self.workspace, "read_prompt", "USER")
        user_body = _prompt_body(user_raw) if user_raw else ""
        if user_body:
            prompt += f"\n\n## User information\n{user_body}"

        memory_raw = await ws_call(self.workspace, "read_prompt", "MEMORY")
        memory_body = _prompt_body(memory_raw) if memory_raw else ""
        if memory_body:
            prompt += f"\n\n## Memory\n{memory_index_context(memory_body)}"

        if context:
            prompt += f"\n\n## Task context\n{context}"

        now = datetime.now(UTC)
        prompt += f"\n\nCurrent time:{now.strftime('%Y-%m-%d %H:%M UTC')}"

        return prompt

    def _build_tools(self) -> list:
        plugin_names = list(dict.fromkeys(DEFAULT_SUB_AGENT_TOOLS + self.config.required_tools))
        tools = initialize_plugins(plugin_names, user_id=self.user_id, workspace=self.workspace)

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
        _ensure_imports()
        all_skills = self.skill_manager.get_all_skills(self.user_id)

        for skill_name in self.config.skills:
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

            synthetic_id = f"skill_autoload_{skill.name}"
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

    _COMPLETION_MARKERS = ["Assessment summary", "Subject:", "collapsible", "report_preview"]

    async def _check_completion(self, final_text: str) -> bool:
        """Use LLM to judge if the sub-agent has completed its task."""
        if not final_text or len(final_text) < 20:
            return False
        try:
            if PROXY_SERVER:
                client = AsyncOpenAI(
                    api_key=OPENAI_API_KEY,
                    base_url=OPENAI_API_BASE,
                    http_client=httpx.AsyncClient(proxy=PROXY_SERVER),
                )
            else:
                client = AsyncOpenAI(
                    api_key=OPENAI_API_KEY,
                    base_url=OPENAI_API_BASE,
                )
            tail = final_text[-1500:]
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=COMPRESSION_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Determine whether this assistant output completes the user task.\n"
                                "Complete: a substantive answer such as a report, email, analysis or procedure.\n"
                                "Incomplete: only asking for more information or waiting for user input.\n"
                                "Answer only yes or no."
                            ),
                        },
                        {"role": "user", "content": tail},
                    ],
                    max_completion_tokens=5,
                    temperature=0,
                ),
                timeout=5.0,
            )
            answer = (resp.choices[0].message.content or "").strip()
            completed = answer.casefold().strip(".! ") == "yes"
            logger.info(f"Completion check ({self.config.name}): answer='{answer}' completed={completed}")
            return completed
        except Exception as e:
            logger.warning(f"Completion check failed, fallback to markers: {e}")
            return any(m in final_text for m in self._COMPLETION_MARKERS)

    async def run_stream(self, user_input: str, context: str = "") -> AsyncGenerator[dict, None]:
        _ensure_imports()
        _ensure_sdk_client()

        t_start = time.monotonic()
        self.needs_followup = False
        self._final_text = ""

        # 1. Build system prompt
        system_prompt = await self._build_system_prompt(context)

        # 2. Build tools
        tools = self._build_tools()
        sdk_tools = convert_tools(tools)

        # 3. Load session history
        history = []
        history_data = await ws_call(self.workspace, "history_load", self.session_id)
        if history_data and isinstance(history_data, dict):
            raw = history_data.get("conversation", [])
            history = _sanitize_history(raw)

        # 3b. Compress history if needed
        compressor = HistoryCompressor(
            compression_level=COMPRESSION_LEVEL,
            async_client=AsyncOpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE,
            ),
        )
        if compressor.should_compress(history):
            history = await compressor.compress_history(history)
            logger.info(f"Sub-agent history compressed: {len(history)} messages remaining")

        # 4. Auto-inject configured skills
        await self._inject_skills(history)

        # 5. Create SDK Agent
        agent = _SDKAgent(
            name=f"SubAgent-{self.config.name}",
            instructions=system_prompt,
            model=self.config.model or MODEL,
            tools=sdk_tools,
            model_settings=ModelSettings(temperature=0.5, max_tokens=16000),
            tool_use_behavior="run_llm_again",
        )
        await configure_agent_tools(
            agent,
            loader=MCPToolManager(
                user_id=self.user_id,
                session_id=self.session_id,
                workspace=self.workspace,
            ).create_tools,
            initially_loaded=self.config.required_tools,
        )

        # Add user message to history before running
        history.append({"role": "user", "content": user_input})

        input_messages = _history_to_sdk_input(history)

        # 6. Stream events
        yield {"code": 3, "content": "stream_start"}

        result = None
        try:
            async with asyncio.timeout(self.config.timeout):
                result = Runner.run_streamed(
                    agent,
                    input=input_messages,
                    max_turns=self.config.max_steps,
                )
                async for event in _stream_sdk_events(result, tools):
                    yield event
                    if event.get("code") == 2:
                        self._final_text += event.get("content", "")
        except TimeoutError:
            logger.warning(f"Sub-agent {self.config.name} timed out")
            yield {"code": 2, "content": f"\n\n[Sub-agent timed out({self.config.timeout}s)]"}
        except Exception as e:
            logger.error(f"Sub-agent {self.config.name} error: {e}", exc_info=True)
            yield {"code": 2, "content": f"\n\n[Sub-agent error: {e}]"}

        # 7. Detect needs_followup via LLM (fallback to markers)
        self.needs_followup = not await self._check_completion(self._final_text)

        # 8. Metrics
        elapsed = time.monotonic() - t_start
        yield {
            "code": 6,
            "content": json.dumps(
                {
                    "agent_type": self.config.name,
                    "elapsed_seconds": round(elapsed, 2),
                },
                ensure_ascii=False,
            ),
        }

        yield {"code": 3, "content": "stream_end"}

        # 9. Save session history
        try:
            if result:
                final_history = _sdk_result_to_history(history, result)
            else:
                final_history = history
            history_record = {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "agent_type": self.config.name,
                "timestamp": datetime.now(UTC).isoformat(),
                "conversation": final_history,
                "message_count": len(final_history),
            }
            await ws_call(self.workspace, "history_save", self.session_id, history_record)
        except Exception as e:
            logger.error(f"Sub-agent history save failed: {e}")
