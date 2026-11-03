"""EvoAgent - The evolvable agent core.

Uses OpenAI Agents SDK for the LLM + tool execution loop (ReAct).
EvoAgent handles context enrichment (skills, profile, workspace).
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import uuid4

from agents import ModelSettings, Runner

from configs.prompts import prompts
from configs.settings import (
    COMPRESSION_LEVEL,
    DEFAULT_PLUGINS,
    ENABLE_METRICS,
    MAX_STEPS,
    MODEL,
)
from core.agent_compat import convert_tools
from core.context_compaction import CompactingAgent as _SDKAgent
from core.delegation import automatic_task_id, stream_handoff
from core.mcp import MCPToolManager
from core.plugins import PLUGIN_CLASSES, initialize_plugins
from core.plugins.ask_user import AskUserQuestionTool
from core.plugins.internal import (
    MemoryTool,
    ScheduleManagerTool,
    SkillTool,
    SubAgentParams,
    SubAgentTool,
    UpdateUserProfileTool,
)
from core.plugins.todo_manager import TodoManagerTool
from core.runtime.delegation_context import delegation_snapshot
from core.runtime.events import _attach_delivery_events as _attach_delivery_events
from core.runtime.events import _find_sub_agent_tool, _stream_sdk_events
from core.runtime.execution import SDKRunSession
from core.runtime.history import (
    _history_to_sdk_input,
    _prompt_body,
    _sanitize_history,
    _sdk_result_to_history,
)
from core.runtime.interruption import TurnPersistence, persist_on_interrupt
from core.runtime.questions import QuestionRoutingError, question_tool_behavior, resolve_answer, user_message
from core.runtime.sdk_client import _create_async_client, _ensure_sdk_client
from core.runtime.usage import capture_usage, record_sdk_usage
from core.skills import Skill, skill_to_md
from core.skills.manager import SkillManager
from core.skills.matcher import SkillMatcher
from core.skills.tool_mounting import SkillToolMounter
from core.tool_search import configure_agent_tools
from utils.context_assets import memory_index_context, skill_location_context
from utils.history_compressor import HistoryCompressor
from utils.logger import logger
from utils.skill_history import loaded_skill_names
from utils.usage import current_usage, usage_payload, usage_scope
from utils.workspace import Workspace
from utils.ws_call import ws_call


class EvoAgent:
    """Evolvable agent with skill learning capabilities.

    Uses OpenAI Agents SDK for the ReAct loop.
    Adds: workspace, skill matching, user profile.
    """

    def __init__(
        self,
        user_id: str,
        session_id: str,
        plugins: list[str] = None,
        system_prompt: str = None,
        model: str = None,
        workspace=None,
        task_id: str | None = None,
        answer_to: dict | None = None,
        attachments: dict | None = None,
        original_input: str | None = None,
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.model = model or MODEL
        self.task_id = task_id
        self.answer_to = answer_to
        self.attachments = attachments or {}
        self.original_input = original_input
        self._answer = None
        self._delegation_snapshot = None

        # Per-user workspace (local filesystem)
        self.workspace = workspace or Workspace(user_id)
        self._persistence = TurnPersistence(self.workspace, user_id, session_id)

        # System prompt is resolved lazily in ``load_session`` so the SOUL
        # read (a remote MinIO GET in MINIO mode) joins the same
        # ``asyncio.gather`` as USER / MEMORY / history / skills instead of
        # running synchronously in __init__ on the request hot path.
        self._system_prompt_override = system_prompt
        self.base_system_prompt = ""
        self.system_prompt = ""

        # Components
        self.skill_manager = SkillManager(workspace=self.workspace)
        self.skill_matcher = SkillMatcher(self.skill_manager)

        # Plugins/tools
        if plugins is not None:
            self.plugin_names = plugins
        elif DEFAULT_PLUGINS.lower() == "all":
            self.plugin_names = list(PLUGIN_CLASSES.keys())
        else:
            self.plugin_names = [p.strip() for p in DEFAULT_PLUGINS.split(",") if p.strip()]
        self.tools = initialize_plugins(
            self.plugin_names,
            user_id=user_id,
            session_id=session_id,
            workspace=self.workspace,
        )

        # Internal tools (always loaded)
        self.tools.append(UpdateUserProfileTool(user_id=user_id, workspace=self.workspace))
        self.tools.append(MemoryTool(user_id=user_id, workspace=self.workspace))
        self.tools.append(
            SkillTool(
                user_id=user_id,
                skill_manager=self.skill_manager,
                workspace=self.workspace,
                session_id=session_id,
            )
        )
        self.tools.append(TodoManagerTool())
        self.tools.append(AskUserQuestionTool())
        self.tools.append(ScheduleManagerTool(user_id=user_id, workspace=self.workspace))
        # SubAgentTool gets a callable that returns this agent's live history
        # so spawned sub-agents can opt-in to reading parent context on demand
        # via ParentHistoryTool.
        self.tools.append(
            SubAgentTool(
                user_id=user_id,
                session_id=session_id,
                workspace=self.workspace,
                parent_history_fn=lambda: self.history,
                context_fn=lambda: self._delegation_snapshot,
            )
        )

        self._skill_tools = SkillToolMounter(
            self.tools,
            user_id=user_id,
            session_id=session_id,
            workspace=self.workspace,
        )

        # History compressor
        self._compressor = HistoryCompressor(
            compression_level=COMPRESSION_LEVEL,
            async_client=_create_async_client(),
        )
        self._compressor.compaction_callbacks.append(self._skill_tools.on_context_compacted)

        # State
        self.history: list[dict] = []
        self.matched_skill: Skill | None = None
        self._preloaded_skills: dict[str, Skill] = {}

    # --- Session & Context ---

    async def load_session(self):
        """Load conversation history + context for this session.

        Fires the five workspace reads (history, SOUL, USER, MEMORY,
        top-level skills) concurrently. In MINIO mode each one is a remote
        GET / LIST so collapsing the round-trips dominates pre-stream
        latency — keeping them sequential adds 4 × RTT to every turn.
        ``ws_call`` already trampolines onto the default thread-pool, and
        the synchronous ``get_all_skills`` is wrapped in ``to_thread`` here
        so it joins the same gather rather than blocking the event loop
        afterwards.
        """
        # When the caller passes an explicit ``system_prompt`` override we
        # skip the SOUL read entirely; otherwise it joins the gather.
        soul_task = (
            asyncio.sleep(0, result=self._system_prompt_override)
            if self._system_prompt_override
            else ws_call(self.workspace, "read_prompt", "SOUL")
        )
        history_data, soul_text, user_text, memory_text, top_skills = await asyncio.gather(
            ws_call(self.workspace, "history_load", self.session_id),
            soul_task,
            ws_call(self.workspace, "read_prompt", "USER"),
            ws_call(self.workspace, "read_prompt", "MEMORY"),
            asyncio.to_thread(self.skill_manager.get_all_skills, self.user_id, top_level_only=True),
        )

        if history_data and isinstance(history_data, dict):
            raw = history_data.get("conversation", [])
            self.history = _sanitize_history(raw)
            logger.info(f"Loaded {len(self.history)} messages for session {self.session_id}")

        self._persistence.loaded(lambda: self.history)

        self.base_system_prompt = (
            self._system_prompt_override if self._system_prompt_override else _prompt_body(soul_text)
        )

        user_body = _prompt_body(user_text)
        if user_body:
            self.system_prompt = self.base_system_prompt + f"\n\n## User information\n{user_body}"
        else:
            self.system_prompt = self.base_system_prompt

        memory_body = _prompt_body(memory_text)
        if memory_body:
            self.system_prompt += (
                "\n\n## Memory index\n"
                f"{memory_index_context(memory_body)}\n\n"
                '_Read a memory body with `memory(action="get", title="...")`. '
                "See the long-term memory rules below._"
            )

        self._preloaded_skills = top_skills
        if self._preloaded_skills:
            catalog = "\n".join(
                f"- **{s.name}**: {s.description} (Triggers: {', '.join(s.triggers)})"
                for s in self._preloaded_skills.values()
            )
            self.system_prompt += f"\n\n## Available skills\n{catalog}"

        # When core profile fields are empty, append a strong reminder at the very end
        # so LLM doesn't forget to update profile while executing skills
        if self._check_profile_incomplete(user_body):
            self.system_prompt += (
                "\n\n## ⚠️ Incomplete user profile\n"
                "Core profile fields such as products, company and markets are empty.\n"
                "When the user explicitly describes their business, update the relevant fields with `update_user_profile`.\n"
                "Do not infer profile facts from incidental task content. "
                "Confirm repeated product signals with the user before updating their profile."
            )

    @staticmethod
    def _check_profile_incomplete(user_body: str) -> bool:
        """Check if core trade profile fields are all empty."""
        if not user_body:
            return True
        core_fields = ["Main products", "Company name", "Target markets"]
        for field in core_fields:
            idx = user_body.find(field + ":")
            if idx == -1:
                continue
            value_start = idx + len(field) + 1
            newline = user_body.find("\n", value_start)
            value = (
                user_body[value_start:newline].strip() if newline != -1 else user_body[value_start:].strip()
            )
            if value and not value.startswith("("):
                return False  # At least one core field has a value
        return True

    @staticmethod
    def _is_skill_meta_request(user_input: str) -> bool:
        """Detect if the user is asking about skill management (create/edit/delete/list),
        not requesting execution of an existing skill."""

        # Match explicit skill-management requests rather than workflow execution.
        meta_pattern = (
            r"\b(create|add|save|modify|edit|update|delete|remove|view|list|manage)\b.{0,30}\bskills?\b"
        )
        return bool(re.search(meta_pattern, user_input, re.IGNORECASE))

    async def _match_skill(self, user_input: str) -> Skill | None:
        """Try to match user input against preloaded skills."""
        self._skill_injected = False

        # Skip matching when user is managing skills (create/edit/delete)
        if self._is_skill_meta_request(user_input):
            logger.info("Skill matching skipped: user intent is skill management")
            return None

        result = await self.skill_matcher.match(
            user_input,
            self.user_id,
            skills=self._preloaded_skills,
        )
        if result.skill:
            logger.info(
                f"Skill matched: {result.skill.name} "
                f"(type={result.match_type}, confidence={result.confidence:.2f})"
            )
            self.matched_skill = result.skill
            self._match_result = result
            if result.skill.requires_sub_agent:
                return result.skill
            if not self._skill_in_history(result.skill.name):
                # First activation this session: inject into history so instructions
                # persist across turns (mirroring skill get), with natural
                # expiry via history compression.
                self.skill_manager.increment_usage(self.user_id, result.skill.name)
                self._inject_skill_into_history(result.skill)
                self._skill_injected = True
            await self._skill_tools.on_loaded(result.skill.name, skill_to_md(result.skill))
        return result.skill

    async def _handle_sub_agent_handoff(self, user_input: str, *, params=None):
        """Automatic routing uses the same task execution and receipt protocol."""
        started = time.monotonic()
        skill = self._match_result.skill if self._match_result and params is None else None
        if params is None:
            params = SubAgentParams(
                task=user_input,
                agent_type=skill.sub_agent_name or skill.name,
                task_id=self.task_id or automatic_task_id(skill.name),
            )
        # Parent-history reads must see this turn, just as explicit delegation does.
        self.history.append(
            user_message(user_input, getattr(self, "_answer", None), getattr(self, "attachments", None))
        )
        if getattr(self, "_persistence", None):
            self._persistence.user_message_added()
        yield {"code": 3, "content": "stream_start"}
        tool = _find_sub_agent_tool(self.tools)
        async with aclosing(stream_handoff(tool, params, skill, self.history)) as events:
            async for event in events:
                yield event
        await self._post_session()
        if ENABLE_METRICS:
            yield {
                "code": 6,
                "content": json.dumps(
                    {
                        "total_ms": int((time.monotonic() - started) * 1000),
                        "skill": {"name": skill.name, "type": "handoff"} if skill else None,
                        "task_id": params.task_id,
                        **usage_payload(),
                    }
                ),
            }
        yield {"code": 3, "content": "stream_end"}

    def _skill_in_history(self, skill_name: str) -> bool:
        """Check if skill instructions already exist in history as a skill tool result.

        Matches the fixed header produced by _format_skill_content() and
        SkillTool._action_get(), covering both synthetic injections and
        genuine agent-initiated skill get calls.
        """
        return skill_name in loaded_skill_names(self.history)

    def _format_skill_content(self, skill: "Skill") -> str:
        """Format skill content matching SkillTool._action_get() output."""
        result = f"Skill document `{skill.name}/SKILL.md`:\n\n```markdown\n{skill.instructions}\n```"
        result += f"\n\n{skill_location_context(skill.name)}"
        if skill.required_tools:
            result += f"\n\nRequired tools (required_tools): {', '.join(skill.required_tools)}"
        if skill.references:
            result += f"\n\nAvailable references ({len(skill.references)}):"
            for filename, content in skill.references.items():
                result += f"\n- `{filename}` ({len(content)}characters) → skill(action='get_ref', name='{skill.name}', ref_filename='{filename}')"
        return result

    def _inject_skill_into_history(self, skill: "Skill") -> None:
        """Append a synthetic skill get call+result to history.

        Appended after existing history so it appears between prior conversation
        and the current user message. The agent sees it in pure_history before
        processing user_input, so skill instructions are available this turn.
        Mirrors skill(get); old entries expire via history compression.
        """
        synthetic_id = f"skill_autoload_{uuid4().hex}"
        self.history.extend(
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
                    "content": self._format_skill_content(skill),
                    "name": "skill",
                    "tool_call_id": synthetic_id,
                },
            ]
        )

    def _reorder_synthetic_skill_in_history(self):
        """Move synthetic skill entries after the user message for correct display order.

        During execution, synthetics are placed BEFORE user_input so the agent
        sees skill instructions first.  For saved history we swap them to AFTER
        the user message so the frontend renders:
            user msg → skill card → agent response.
        """
        for i in range(len(self.history) - 2):
            msg = self.history[i]
            if (
                msg.get("role") == "assistant"
                and msg.get("tool_calls")
                and any(tc.get("id", "").startswith("skill_autoload_") for tc in msg["tool_calls"])
            ):
                if (
                    self.history[i + 1].get("role") == "tool"
                    and i + 2 < len(self.history)
                    and self.history[i + 2].get("role") == "user"
                ):
                    synthetic = [self.history.pop(i), self.history.pop(i)]
                    self.history[i + 1 : i + 1] = synthetic
                break

    def _inject_history_todos(self):
        """Inject last todo snapshot from session history into todo_manager.

        Scans history in reverse for the last todo_manager tool result.
        If found and has incomplete tasks, sets tool._history_tasks for
        lazy restore on next update call.
        """
        last_tasks = None
        for msg in reversed(self.history):
            if msg.get("role") != "tool":
                continue
            try:
                result = json.loads(msg.get("content", ""))
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("tasks"), list)
                    and result["tasks"]
                    and "status" in result["tasks"][0]
                ):
                    last_tasks = result["tasks"]
                    break
            except (json.JSONDecodeError, TypeError):
                continue
        if not last_tasks:
            return
        for tool in self.tools:
            if getattr(tool, "name", "") == "todo_manager":
                tool._history_tasks = last_tasks
                break

    # --- Agent Execution ---

    async def _create_sdk_agent(self) -> "_SDKAgent":
        """Create an OpenAI Agents SDK Agent for LLM + tool loop execution."""
        _ensure_sdk_client()

        sdk_tools = convert_tools(self.tools)
        mcp_manager = MCPToolManager(
            user_id=self.user_id,
            session_id=self.session_id,
            workspace=self.workspace,
        )

        agent = _SDKAgent(
            name="EvoAgent",
            instructions=self.system_prompt,
            model=self.model,
            tools=sdk_tools,
            model_settings=ModelSettings(
                temperature=0.5,
                max_tokens=16000,
                top_p=0.7,
            ),
            tool_use_behavior=question_tool_behavior(),
        )
        catalog = await configure_agent_tools(
            agent,
            loader=mcp_manager.create_tools,
        )
        await self._skill_tools.bind(agent, catalog)
        return agent

    async def _stream_agent_events(self, user_input: str) -> AsyncGenerator[dict, None]:
        """Run the SDK agent with streaming and yield SSE-compatible events."""
        # Add user message to history BEFORE running agent
        self.history.append(
            user_message(user_input, getattr(self, "_answer", None), getattr(self, "attachments", None))
        )
        if getattr(self, "_persistence", None):
            self._persistence.user_message_added()

        input_messages = _history_to_sdk_input(self.history)

        agent = await self._create_sdk_agent()
        session = SDKRunSession(
            agent,
            self._compressor,
            self.history,
            self.tools,
            on_history=lambda history: setattr(self, "history", history),
            event_stream=_stream_sdk_events,
        )
        if getattr(self, "_persistence", None):
            self._persistence.user_message_added()
            self._persistence.loaded(session.snapshot)
        session.start(Runner.run_streamed, agent, input=input_messages, max_turns=MAX_STEPS)
        try:
            async with aclosing(session.stream()) as events:
                async for event in events:
                    yield event
        finally:
            self.history = session.snapshot()

    @capture_usage
    @persist_on_interrupt
    async def run_stream(self, user_input: str) -> AsyncGenerator[dict, None]:
        """Run the agent in streaming mode.

        Yields dicts with:
          code: 2 (text), 3 (markers), 4 (tool_call), 5 (tool_result), 6 (metrics)
          content: the content string
        """
        t_start = time.monotonic()

        # 1. Load session data (history + context)
        await self.load_session()

        source_input = self.original_input if self.original_input is not None else user_input
        self._delegation_snapshot = delegation_snapshot(source_input, self.history, self.attachments)

        try:
            self._answer = resolve_answer(self.history, self.answer_to)
        except QuestionRoutingError as exc:
            yield {"code": 0, "content": str(exc)}
            yield {"code": 3, "content": "stream_end"}
            return
        self._delegation_snapshot["answer_to"] = self._answer
        if self._answer and self._answer.get("task_id"):
            self._match_result = None
            params = SubAgentParams(
                task=user_input,
                agent_type=self._answer["agent_type"],
                task_id=self._answer["task_id"],
                context="Answer to the saved question: " + json.dumps(self._answer, ensure_ascii=False),
            )
            async for event in self._handle_sub_agent_handoff(user_input, params=params):
                yield event
            return

        # 2. Compress history if needed (before skill injection)
        if self._compressor.should_compress(self.history):
            self.history = await self._compressor.compress_history(self.history)
        await self._skill_tools.restore(self.history)

        # 3. Skill matching (appends synthetic messages after compressed history)
        self._match_result = None
        if not self._answer:
            await self._match_skill(user_input)

        # 3.5. Trigger-based sub-agent handoff (paper Appendix A, Phase 2.5).
        # If the matched skill declares `requires_sub_agent`, bypass main agent:
        # a dedicated sub-agent (configured by the skill's sub_agent_* fields)
        # handles the turn and its events are streamed directly to the client.
        if self._match_result and self._match_result.skill and self._match_result.skill.requires_sub_agent:
            async for event in self._handle_sub_agent_handoff(user_input):
                yield event
            return

        # 4. Inject last todo snapshot for cross-turn resume
        self._inject_history_todos()

        # 5. Run agent via OpenAI Agents SDK
        yield {"code": 3, "content": "stream_start"}

        # Emit skill injection as tool card (only when actually injected this turn)
        if self._match_result and self._match_result.skill and self._skill_injected:
            sk = self._match_result.skill
            synthetic_id = self.history[-2]["tool_calls"][0]["id"]
            yield {
                "code": 4,
                "content": json.dumps(
                    {
                        "id": synthetic_id,
                        "name": "skill",
                        "arguments": json.dumps({"action": "get", "name": sk.name}, ensure_ascii=False),
                    },
                    ensure_ascii=False,
                ),
            }
            yield {
                "code": 5,
                "content": json.dumps(
                    {
                        "id": synthetic_id,
                        "name": "skill",
                        "result": f"loadedSkill {sk.name}",
                    },
                    ensure_ascii=False,
                ),
            }

        steps = 0
        tools_used = set()
        try:
            async with aclosing(self._stream_agent_events(user_input)) as events:
                async for event in events:
                    content = event.get("content", "")
                    if event.get("code") == 4:
                        steps += 1
                        try:
                            info = json.loads(content)
                            tools_used.add(info.get("name", ""))
                        except Exception:
                            pass
                    yield event
        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            yield {"code": 2, "content": f"Agent error: {str(e)}"}

        # 5. Emit metrics (code 6)
        if ENABLE_METRICS:
            from utils.token_counter import count_messages_tokens

            skill_info = None
            if self._match_result and self._match_result.skill:
                skill_info = {
                    "name": self._match_result.skill.name,
                    "type": self._match_result.match_type,
                }
            yield {
                "code": 6,
                "content": json.dumps(
                    {
                        "total_ms": int((time.monotonic() - t_start) * 1000),
                        "steps": steps,
                        "tools": list(tools_used),
                        "tokens": count_messages_tokens(self.history),
                        "context_tokens": self._compressor.calculate_history_tokens(self.history),
                        **usage_payload(),
                        "skill": skill_info,
                    }
                ),
            }

        # 5. Save session before stream ends (must complete before Bridge cleanup)
        await self._post_session()

        yield {"code": 3, "content": "stream_end"}

    async def _post_session(self):
        """Delegate session storage to the shared persistence component."""
        persistence = getattr(self, "_persistence", None) or TurnPersistence(
            self.workspace, self.user_id, self.session_id
        )
        try:
            await persistence.save(self.history)
        except Exception:
            logger.exception("Session history save failed")


# --- Standalone Session Update Agent ---


async def run_session_update_stream(
    user_id: str, session_id: str, workspace: "Workspace" = None
) -> AsyncGenerator[dict, None]:
    """Review a session with its own usage scope, separate from online turns."""
    with usage_scope():
        async with aclosing(_run_session_update_stream(user_id, session_id, workspace)) as events:
            async for event in events:
                yield event


async def _run_session_update_stream(
    user_id: str, session_id: str, workspace: "Workspace" = None
) -> AsyncGenerator[dict, None]:
    """Lightweight agent that reviews session history and updates user profile/memory.

    Bypasses EvoAgent entirely — no SkillManager, no plugins, no skill matching.
    Only loads: session history + SESSION_UPDATE prompt + 2 internal tools.
    """
    # 1. Load session history
    ws = workspace or Workspace(user_id)
    history_data = await ws_call(ws, "history_load", session_id)
    if not history_data or not isinstance(history_data, dict):
        yield {"code": 2, "content": "The current session is empty; no update is needed."}
        return

    history = _sanitize_history(history_data.get("conversation", []))
    if not history:
        yield {"code": 2, "content": "The current session is empty; no update is needed."}
        return

    # 2. Load prompt (SESSION_UPDATE + SKILL_EXTRACT appended)
    update_prompt = prompts.get_body("SESSION_UPDATE")
    if not update_prompt:
        yield {"code": 2, "content": "SESSION_UPDATE prompt not found."}
        return
    skill_extract_prompt = prompts.get_body("SKILL_EXTRACT")
    if skill_extract_prompt:
        update_prompt += f"\n\n---\n\n{skill_extract_prompt}"

    # 2b. Inject current user profile for comparison
    user_body = _prompt_body(await ws_call(ws, "read_prompt", "USER"))
    if user_body:
        update_prompt += f"\n\n## Current user profile\n{user_body}"

    # 2c. Inject recent session summaries for behavior analysis
    sessions = await ws_call(ws, "list_sessions")
    if sessions:
        recent = sessions[:10]
        summaries = "\n".join(
            f"- [{s.get('timestamp', '')}] {s.get('preview', '(No summary)')}" for s in recent
        )
        update_prompt += f"\n\n## Recent conversations\n{summaries}"

    # 3. Tools: profile + memory + full skill manager
    tools = [
        UpdateUserProfileTool(user_id=user_id, workspace=ws),
        MemoryTool(user_id=user_id, workspace=ws),
        SkillTool(
            user_id=user_id,
            skill_manager=SkillManager(workspace=ws),
            workspace=ws,
            session_id=session_id,
        ),
    ]

    # 4. Create SDK agent
    _ensure_sdk_client()
    sdk_tools = convert_tools(tools)

    agent = _SDKAgent(
        name="SessionUpdater",
        instructions=update_prompt,
        model=MODEL,
        tools=sdk_tools,
        model_settings=ModelSettings(temperature=0.4, max_tokens=16000),
        tool_use_behavior="run_llm_again",
    )

    input_messages = _history_to_sdk_input(history)
    input_messages.append(
        {
            "role": "user",
            "content": "Review the conversation above and update the user profile and memory as warranted.",
        }
    )

    # 5. Stream events
    yield {"code": 3, "content": "stream_start"}
    result = None
    run_id = uuid4().hex
    try:
        result = Runner.run_streamed(agent, input=input_messages, max_turns=10)
        async with aclosing(_stream_sdk_events(result, tools)) as events:
            async for event in events:
                yield event
    except Exception as e:
        logger.error(f"Update agent failed: {e}")
        yield {"code": 2, "content": f"Update failed: {str(e)}"}
    finally:
        record_sdk_usage(current_usage(), result, run_id=run_id, model=MODEL, category="session_review")

    if result is None:
        yield {"code": 3, "content": "stream_end"}
        return

    # Save updated history
    try:
        updated_history = _sdk_result_to_history(history, result)
        history_record = {
            "user_id": user_id,
            "session_id": session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "conversation": updated_history,
            "message_count": len(updated_history),
        }
        await ws_call(ws, "history_save", session_id, history_record)
        logger.info(f"Saved {len(updated_history)} messages (incl. update context) to session {session_id}")
    except Exception as e:
        logger.error(f"Failed to save update context to session: {e}")

    yield {"code": 6, "content": json.dumps(usage_payload(), ensure_ascii=False)}
    yield {"code": 3, "content": "stream_end"}
    logger.info(f"Session update completed for user {user_id}")
