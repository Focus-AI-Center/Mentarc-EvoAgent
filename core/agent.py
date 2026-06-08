"""EvoAgent - The evolvable agent core.

Uses OpenAI Agents SDK for the LLM + tool execution loop (ReAct).
EvoAgent handles context enrichment (skills, profile, workspace).
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
from agents import Agent as _SDKAgent
from agents import ModelSettings, Runner, set_default_openai_client
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem
from openai import AsyncOpenAI  # used by _ensure_sdk_client

from configs.prompts import prompts
from configs.settings import (
    COMPRESSION_LEVEL,
    DEFAULT_PLUGINS,
    ENABLE_METRICS,
    MAX_STEPS,
    MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from core.agent_compat import convert_tools
from core.mcp import MCPToolManager
from core.plugins import PLUGIN_CLASSES, initialize_plugins
from core.plugins.ask_user import AskUserQuestionTool
from core.plugins.internal import (
    ScheduleManagerTool,
    SkillManagerTool,
    SubAgentTool,
    UpdateMemoryTool,
    UpdateUserProfileTool,
)
from core.plugins.todo_manager import TodoManagerTool
from core.skills import Skill
from core.skills.manager import SkillManager
from core.skills.matcher import SkillMatcher
from utils.history_compressor import HistoryCompressor
from utils.logger import logger
from utils.workspace import Workspace
from utils.ws_call import ws_call

# --- SDK client initialization ---

_sdk_client_initialized = False


def _create_async_client() -> AsyncOpenAI:
    """Create a proxy-aware AsyncOpenAI client (for HistoryCompressor)."""
    if PROXY_SERVER:
        return AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
            http_client=httpx.AsyncClient(proxy=PROXY_SERVER),
        )
    return AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)


def _ensure_sdk_client():
    """Set up the default OpenAI client for the Agents SDK (once)."""
    global _sdk_client_initialized
    if _sdk_client_initialized:
        return
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
    set_default_openai_client(client)
    _sdk_client_initialized = True


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
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.model = model or MODEL

        # Per-user workspace (local filesystem)
        self.workspace = workspace or Workspace(user_id)

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
        self.tools.append(UpdateMemoryTool(user_id=user_id, workspace=self.workspace))
        self.tools.append(
            SkillManagerTool(
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
                workspace=self.workspace,
                parent_history_fn=lambda: self.history,
            )
        )

        # History compressor
        self._compressor = HistoryCompressor(
            compression_level=COMPRESSION_LEVEL,
            async_client=_create_async_client(),
        )

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

        self.base_system_prompt = (
            self._system_prompt_override if self._system_prompt_override else _prompt_body(soul_text)
        )

        user_body = _prompt_body(user_text)
        if user_body:
            self.system_prompt = self.base_system_prompt + f"\n\n## 用户信息\n{user_body}"
        else:
            self.system_prompt = self.base_system_prompt

        memory_body = _prompt_body(memory_text)
        if memory_body:
            self.system_prompt += (
                "\n\n## 记忆索引\n"
                f"{memory_body}\n\n"
                '_要某条记忆的完整正文，调用 `update_memory(action="get", title="...")`；'
                "详细规则见下方「长期记忆维护」。_"
            )

        self._preloaded_skills = top_skills
        if self._preloaded_skills:
            catalog = "\n".join(
                f"- **{s.name}**: {s.description} (触发词: {', '.join(s.triggers)})"
                for s in self._preloaded_skills.values()
            )
            self.system_prompt += f"\n\n## 已掌握技能\n{catalog}"

        # When core profile fields are empty, append a strong reminder at the very end
        # so LLM doesn't forget to update profile while executing skills
        if self._check_profile_incomplete(user_body):
            self.system_prompt += (
                "\n\n## ⚠️ 用户画像待完善\n"
                "检测到用户画像的核心字段（主营产品、公司名称、目标市场等）为空。\n"
                "当用户**主动介绍自己的业务信息**时，调用 `update_user_profile` 更新对应字段。\n"
                "注意：单次任务中出现的产品信息不要直接写入画像；"
                "同一产品出现≥2次时询问确认，≥3次时直接追加到主营产品。"
            )

    @staticmethod
    def _check_profile_incomplete(user_body: str) -> bool:
        """Check if core trade profile fields are all empty."""
        if not user_body:
            return True
        core_fields = ["主营产品", "公司名称", "目标市场"]
        for field in core_fields:
            idx = user_body.find(field + "：")
            if idx == -1:
                continue
            value_start = idx + len(field) + 1
            newline = user_body.find("\n", value_start)
            value = (
                user_body[value_start:newline].strip() if newline != -1 else user_body[value_start:].strip()
            )
            if value and not value.startswith("（"):
                return False  # At least one core field has a value
        return True

    @staticmethod
    def _is_skill_meta_request(user_input: str) -> bool:
        """Detect if the user is asking about skill management (create/edit/delete/list),
        not requesting execution of an existing skill."""
        import re

        # Match patterns like: 创建/新建/添加/修改/删除/查看 + skill/技能
        meta_pattern = r"(创建|新建|添加|保存|修改|编辑|更新|删除|移除|查看|列出|管理).{0,6}(skill|技能)"
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
            self._ensure_tools(result.skill.required_tools)
            if not self._skill_in_history(result.skill.name):
                # First activation this session: inject into history so instructions
                # persist across turns (mirroring skill_manager get), with natural
                # expiry via history compression.
                self.skill_manager.increment_usage(self.user_id, result.skill.name)
                self._inject_skill_into_history(result.skill)
                self._skill_injected = True
        return result.skill

    async def _handle_sub_agent_handoff(self, user_input: str):
        """Run a trigger-based sub-agent handoff (paper Appendix A Phase 2.5).

        Streams the sub-agent's events directly to the caller, then appends the
        resulting messages to the main agent's history so future turns see the
        handoff outcome. The main agent's ReAct loop is bypassed for this turn.
        """
        from core.sub_agent import SubAgentRunner

        skill = self._match_result.skill
        logger.info(
            f"Phase 2.5 handoff: skill '{skill.name}' triggered sub-agent "
            f"'{skill.sub_agent_name or skill.name}'"
        )

        yield {"code": 3, "content": "stream_start"}

        runner = SubAgentRunner.from_skill(
            skill,
            user_id=self.user_id,
            session_id=self.session_id,
            workspace=self.workspace,
            parent_history_fn=lambda: self.history,
        )

        # Relay all sub-agent events transparently and capture final text so we
        # can merge the turn into the main agent's history.
        final_text_parts: list[str] = []
        try:
            async for event in runner.run_stream(user_input):
                if event.get("code") == 2:
                    final_text_parts.append(str(event.get("content", "")))
                # Don't forward sub-agent's stream_start/stream_end — we wrap it.
                if event.get("code") == 3 and event.get("content") in ("stream_start", "stream_end"):
                    continue
                yield event
        except Exception as e:
            logger.error(f"Sub-agent handoff failed: {e}")
            yield {"code": 2, "content": f"Sub-agent error: {str(e)}"}

        # Merge handoff outcome into main history
        final_text = "".join(final_text_parts).strip()
        self.history.append({"role": "user", "content": user_input})
        if final_text:
            self.history.append({"role": "assistant", "content": final_text})

        yield {"code": 3, "content": "stream_end"}

        # Persist history so next turn sees the handoff
        await self._post_session()

    def _skill_in_history(self, skill_name: str) -> bool:
        """Check if skill instructions already exist in history as a skill_manager tool result.

        Matches the fixed header produced by _format_skill_content() and
        SkillManagerTool._action_get(), covering both synthetic injections and
        genuine agent-initiated skill_manager get calls.
        """
        skill_header = f"技能文档 `{skill_name}/SKILL.md`"
        return any(
            msg.get("role") == "tool"
            and msg.get("name") == "skill_manager"
            and skill_header in msg.get("content", "")
            for msg in self.history
        )

    def _format_skill_content(self, skill: "Skill") -> str:
        """Format skill content matching SkillManagerTool._action_get() output."""
        result = f"技能文档 `{skill.name}/SKILL.md`:\n\n```markdown\n{skill.instructions}\n```"
        if skill.required_tools:
            result += f"\n\n所需工具 (required_tools): {', '.join(skill.required_tools)}"
        if skill.references:
            result += f"\n\n可用支撑文件 ({len(skill.references)}):"
            for filename, content in skill.references.items():
                result += f"\n- `{filename}` ({len(content)}字符) → skill_manager(action='get_ref', name='{skill.name}', ref_filename='{filename}')"
        return result

    def _inject_skill_into_history(self, skill: "Skill") -> None:
        """Append a synthetic skill_manager get call+result to history.

        Appended after existing history so it appears between prior conversation
        and the current user message. The agent sees it in pure_history before
        processing user_input, so skill instructions are available this turn.
        Mirrors skill_manager(get); old entries expire via history compression.
        """
        synthetic_id = f"skill_autoload_{skill.name}"
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
                                "name": "skill_manager",
                                "arguments": json.dumps({"action": "get", "name": skill.name}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": self._format_skill_content(skill),
                    "name": "skill_manager",
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

    def _ensure_tools(self, required_tools: list[str]):
        """Ensure all required tools for a skill are loaded."""
        current_names = {getattr(t, "name", "") for t in self.tools}
        missing = [t for t in required_tools if t not in current_names]
        if missing:
            new_tools = initialize_plugins(missing, user_id=self.user_id, workspace=self.workspace)
            self.tools.extend(new_tools)
            logger.info(f"Auto-loaded tools for skill: {missing}")

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
        mcp_tools = await MCPToolManager(
            user_id=self.user_id,
            session_id=self.session_id,
            workspace=self.workspace,
        ).create_tools(existing_names={getattr(t, "name", "") for t in sdk_tools})
        sdk_tools.extend(mcp_tools)

        return _SDKAgent(
            name="EvoAgent",
            instructions=self.system_prompt,
            model=self.model,
            tools=sdk_tools,
            model_settings=ModelSettings(
                temperature=0.5,
                max_tokens=16000,
                top_p=0.7,
            ),
            tool_use_behavior="run_llm_again",
        )

    async def _stream_agent_events(self, user_input: str) -> AsyncGenerator[dict, None]:
        """Run the SDK agent with streaming and yield SSE-compatible events."""
        # Build input messages: history + new user message
        input_messages = _history_to_sdk_input(self.history)
        input_messages.append({"role": "user", "content": user_input})

        # Add user message to history BEFORE running agent
        self.history.append({"role": "user", "content": user_input})

        agent = await self._create_sdk_agent()
        result = Runner.run_streamed(agent, input=input_messages, max_turns=MAX_STEPS)

        async for event in _stream_sdk_events(result, self.tools):
            yield event

        # Append agent's response to history
        self.history = _sdk_result_to_history(self.history, result)

    async def run_stream(self, user_input: str) -> AsyncGenerator[dict, None]:
        """Run the agent in streaming mode.

        Yields dicts with:
          code: 2 (text), 3 (markers), 4 (tool_call), 5 (tool_result), 6 (metrics)
          content: the content string
        """
        t_start = time.monotonic()

        # 1. Load session data (history + context)
        await self.load_session()

        # 2. Compress history if needed (before skill injection)
        if self._compressor.should_compress(self.history):
            self.history = await self._compressor.compress_history(self.history)

        # 3. Skill matching (appends synthetic messages after compressed history)
        self._match_result = None
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
            synthetic_id = f"skill_autoload_{sk.name}"
            yield {
                "code": 4,
                "content": json.dumps(
                    {
                        "id": synthetic_id,
                        "name": "skill_manager",
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
                        "name": "skill_manager",
                        "result": f"已加载技能 {sk.name}",
                    },
                    ensure_ascii=False,
                ),
            }

        steps = 0
        tools_used = set()
        try:
            async for event in self._stream_agent_events(user_input):
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
                        "skill": skill_info,
                    }
                ),
            }

        # 5. Save session before stream ends (must complete before Bridge cleanup)
        await self._post_session()

        yield {"code": 3, "content": "stream_end"}

    async def _post_session(self):
        """Async post-session tasks: save history."""
        try:
            history_record = {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "conversation": self.history,
                "message_count": len(self.history),
            }
            await ws_call(self.workspace, "history_save", self.session_id, history_record)
            logger.info(f"Saved {len(self.history)} messages to workspace")
        except Exception as e:
            logger.error(f"Post-session tasks failed: {e}")


def _find_sub_agent_tool(tools: list):
    """Find SubAgentTool instance in the tool list (for event flushing)."""
    for t in tools:
        if getattr(t, "name", "") == "sub_agent" and hasattr(t, "_sub_events"):
            return t
    return None


async def _stream_sdk_events(result, tools: list) -> AsyncGenerator[dict, None]:
    """Stream events from OpenAI Agents SDK run and yield SSE-compatible dicts.

    Maps SDK events to existing SSE codes for frontend compatibility:
      code 2: text delta
      code 4: tool_call
      code 5: tool_result
      code 7: schedule_manager
      code 8: html_render
      code 9: todo_manager progress
      code 10: ask_user_question
    """
    from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent

    # Cache call_id → tool_name for matching tool results to their calls
    _call_id_to_name: dict[str, str] = {}

    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            # Raw LLM response — extract text deltas
            data = event.data
            if hasattr(data, "type") and data.type == "response.output_text.delta":
                delta = getattr(data, "delta", "")
                if delta:
                    yield {"code": 2, "content": delta}

        elif isinstance(event, RunItemStreamEvent):
            item = event.item
            if isinstance(item, ToolCallItem):
                # Tool call started
                raw = item.raw_item
                tc_id = getattr(raw, "call_id", "") or ""
                tc_name = getattr(raw, "name", "") or ""
                tc_args = getattr(raw, "arguments", "") or ""
                if tc_id and tc_name:
                    _call_id_to_name[tc_id] = tc_name
                yield {
                    "code": 4,
                    "content": json.dumps(
                        {
                            "id": tc_id,
                            "name": tc_name,
                            "arguments": tc_args[:500],
                        },
                        ensure_ascii=False,
                    ),
                }

            elif isinstance(item, ToolCallOutputItem):
                # Tool result — raw_item is a dict with call_id, output, type
                raw_ri = item.raw_item
                if isinstance(raw_ri, dict):
                    tc_id = raw_ri.get("call_id", "")
                else:
                    tc_id = getattr(raw_ri, "call_id", "") or ""
                tc_name = _call_id_to_name.get(tc_id, "")
                output_str = str(item.output) if item.output else ""

                # Flush sub-agent buffered events before emitting tool result
                if tc_name == "sub_agent":
                    sat = _find_sub_agent_tool(tools)
                    if sat and sat._sub_events:
                        for sub_evt in sat._sub_events:
                            yield sub_evt
                        sat._sub_events.clear()

                # Detect html_render
                if tc_name == "html_render":
                    yield {
                        "code": 5,
                        "content": json.dumps(
                            {
                                "id": tc_id,
                                "name": tc_name,
                                "result": "HTML 页面已生成，正在渲染预览...",
                            },
                            ensure_ascii=False,
                        ),
                    }
                    yield {"code": 8, "content": output_str}
                else:
                    yield {
                        "code": 5,
                        "content": json.dumps(
                            {
                                "id": tc_id,
                                "name": tc_name,
                                "result": output_str[:1000],
                            },
                            ensure_ascii=False,
                        ),
                    }

                # Detect schedule_manager → code 7
                if tc_name == "schedule_manager":
                    try:
                        sched_data = json.loads(output_str)
                        if sched_data.get("action") in ("create", "delete"):
                            yield {"code": 7, "content": json.dumps(sched_data, ensure_ascii=False)}
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Detect todo_manager → code 9
                if tc_name == "todo_manager":
                    try:
                        todo_data = json.loads(output_str)
                        if "tasks" in todo_data:
                            yield {"code": 9, "content": json.dumps(todo_data, ensure_ascii=False)}
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Detect ask_user_question → code 10
                if tc_name == "ask_user_question":
                    try:
                        ask_data = json.loads(output_str)
                        if ask_data.get("type") == "ask_user":
                            yield {"code": 10, "content": json.dumps(ask_data, ensure_ascii=False)}
                    except (json.JSONDecodeError, TypeError):
                        pass


def _history_to_sdk_input(history: list[dict]) -> list[dict]:
    """Convert saved history to OpenAI Agents SDK input format.

    SDK Responses API only accepts: user, assistant, system, developer.
    Filter out 'tool' messages and simplify 'assistant' tool_call messages.
    """
    messages = []
    for msg in history:
        role = msg.get("role", "")
        if role == "system":
            continue  # SDK handles system prompt via instructions
        if role == "tool":
            continue  # Responses API doesn't support 'tool' role
        content = msg.get("content")
        if content is None:
            content = ""
        # Skip empty assistant messages (tool_call-only messages with no text)
        if role == "assistant" and not str(content).strip() and msg.get("tool_calls"):
            continue
        messages.append({"role": role, "content": str(content)})
    return messages


def _sdk_result_to_history(prev_history: list[dict], result) -> list[dict]:
    """Extract conversation history from SDK run result.

    Appends the new messages from the SDK run to the previous history.
    """
    history = list(prev_history)
    for item in result.new_items:
        if isinstance(item, MessageOutputItem):
            text = ""
            raw = item.raw_item
            # Extract text from the raw response output
            if hasattr(raw, "content") and raw.content:
                for part in raw.content:
                    if hasattr(part, "text"):
                        text += part.text
            if text:
                history.append({"role": "assistant", "content": text})
        elif isinstance(item, ToolCallItem):
            raw = item.raw_item
            history.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": getattr(raw, "call_id", ""),
                            "type": "function",
                            "function": {
                                "name": getattr(raw, "name", ""),
                                "arguments": getattr(raw, "arguments", ""),
                            },
                        }
                    ],
                }
            )
        elif isinstance(item, ToolCallOutputItem):
            raw_ri = item.raw_item
            tc_id = (
                raw_ri.get("call_id", "")
                if isinstance(raw_ri, dict)
                else getattr(raw_ri, "call_id", "") or ""
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": str(item.output) if item.output else "",
                }
            )
    return history


_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _prompt_body(raw: str) -> str:
    """Strip YAML frontmatter from a prompt file and return just the body."""
    return _FRONTMATTER_RE.sub("", raw, count=1).strip()


def _sanitize_history(raw: list[dict]) -> list[dict]:
    """Sanitize saved history for reloading.

    - Filters out system messages (agent handles system prompt via instructions)
    - Converts None values to empty strings
    - Preserves complex types (tool_calls as list)
    """
    cleaned: list[dict] = []
    for msg in raw:
        if msg.get("role") == "system":
            continue
        clean = dict(msg)
        for k, v in clean.items():
            if v is None:
                clean[k] = ""
        cleaned.append(clean)
    return cleaned


# --- Standalone Session Update Agent ---


async def run_session_update_stream(
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
        yield {"code": 2, "content": "当前会话为空，无需更新。"}
        return

    history = _sanitize_history(history_data.get("conversation", []))
    if not history:
        yield {"code": 2, "content": "当前会话为空，无需更新。"}
        return

    # 2. Load prompt (SESSION_UPDATE + SKILL_EXTRACT appended)
    update_prompt = prompts.get_body("SESSION_UPDATE")
    if not update_prompt:
        yield {"code": 2, "content": "SESSION_UPDATE prompt 未找到。"}
        return
    skill_extract_prompt = prompts.get_body("SKILL_EXTRACT")
    if skill_extract_prompt:
        update_prompt += f"\n\n---\n\n{skill_extract_prompt}"

    # 2b. Inject current user profile for comparison
    user_body = _prompt_body(await ws_call(ws, "read_prompt", "USER"))
    if user_body:
        update_prompt += f"\n\n## 当前用户画像\n{user_body}"

    # 2c. Inject recent session summaries for behavior analysis
    sessions = await ws_call(ws, "list_sessions")
    if sessions:
        recent = sessions[:10]
        summaries = "\n".join(f"- [{s.get('timestamp', '')}] {s.get('preview', '(无摘要)')}" for s in recent)
        update_prompt += f"\n\n## 近期会话记录\n{summaries}"

    # 3. Tools: profile + memory + full skill manager
    tools = [
        UpdateUserProfileTool(user_id=user_id, workspace=ws),
        UpdateMemoryTool(user_id=user_id, workspace=ws),
        SkillManagerTool(
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
        model_settings=ModelSettings(temperature=0.4, max_tokens=6000),
        tool_use_behavior="run_llm_again",
    )

    input_messages = _history_to_sdk_input(history)
    input_messages.append({"role": "user", "content": "请回顾以上对话，按需更新用户画像和记忆。"})

    # 5. Stream events
    yield {"code": 3, "content": "stream_start"}
    try:
        result = Runner.run_streamed(agent, input=input_messages, max_turns=10)
        async for event in _stream_sdk_events(result, tools):
            yield event
    except Exception as e:
        logger.error(f"Update agent failed: {e}")
        yield {"code": 2, "content": f"更新失败: {str(e)}"}

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

    yield {"code": 3, "content": "stream_end"}
    logger.info(f"Session update completed for user {user_id}")
