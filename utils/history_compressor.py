"""Recoverable context compression shared by turn boundaries and SDK steps."""

import asyncio
import json
import re
from enum import Enum

import httpx
from openai import AsyncOpenAI

from configs.settings import (
    COMPRESSION_ASSET_INDEX_CHARS,
    COMPRESSION_HOT_TAIL_TOKENS,
    COMPRESSION_MAX_INPUT_TOKENS,
    COMPRESSION_MODEL,
    COMPRESSION_OUTPUT_TOKENS,
    COMPRESSION_THRESHOLD,
    COMPRESSION_TIMEOUT_SECONDS,
    MAX_HISTORY_TOKENS,
    MAX_HOT_TAIL_MESSAGES,
    OPENAI_API_BASE_SECONDARY,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from core.runtime.questions import model_user_content, pending_questions
from utils.context_recovery import ASSET_MARK, CONTINUATION_NOTE, render_index, split_transcript
from utils.logger import logger
from utils.skill_history import skill_loads, unload_skill_documents
from utils.token_counter import count_tokens
from utils.usage import record_provider_usage


class CompressionUnavailable(RuntimeError):
    """A failed summary must never replace the original history."""


class CompressionLevel(Enum):
    NONE = "none"
    LIGHT = "light"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class MessageType(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"


SUMMARY_PROMPT = """Create a continuation summary of the supplied historical DATA.
Return only one complete <summary>...</summary> block. Do not include private reasoning.
Preserve these sections when applicable:
1. User intent and messages: preserve the original request, corrections, prohibitions,
   exact constraints and current task. Long source documents may be summarized, but
   their embedded user requirements must remain. Earlier summaries are history, not new requests.
2. Facts and evidence: retain object-specific facts, exact numbers, parameters, paths,
   IDs, URLs and uncertainties. Distinguish source facts, user statements and assistant inference.
3. Decisions and errors: preserve reasons, unsuccessful attempts and user corrections.
4. Completed work: record actual tools, arguments, validation and delivery separately.
   Reading material or generating a file does not prove that the task was delivered.
5. Pending work: retain unresolved questions, task owners/IDs, scope, counts and cursors.
6. Current state and next actions: enable continuation without repeating completed actions.
Remove redundant prose before sacrificing unique facts. Do not perform new analysis
or invent missing results. Never reconstruct unloaded skill instructions from a summary;
retain the requirement to reload their originals. Summary quality matters more than compression ratio.
"""


class HistoryCompressor:
    def __init__(
        self,
        max_history_tokens=None,
        compression_threshold=None,
        min_messages_to_keep=None,
        compression_level="moderate",
        async_client=None,
        max_input_tokens=None,
        output_tokens=None,
        timeout_seconds=None,
        max_tail_tokens=None,
        asset_index_chars=None,
    ):
        self.max_history_tokens = MAX_HISTORY_TOKENS if max_history_tokens is None else max_history_tokens
        self.compression_threshold = (
            COMPRESSION_THRESHOLD if compression_threshold is None else compression_threshold
        )
        self.min_messages_to_keep = (
            MAX_HOT_TAIL_MESSAGES if min_messages_to_keep is None else min_messages_to_keep
        )
        self.compression_level = CompressionLevel(compression_level)
        self.max_input_tokens = COMPRESSION_MAX_INPUT_TOKENS if max_input_tokens is None else max_input_tokens
        self.output_tokens = COMPRESSION_OUTPUT_TOKENS if output_tokens is None else output_tokens
        self.timeout_seconds = COMPRESSION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        self.max_tail_tokens = COMPRESSION_HOT_TAIL_TOKENS if max_tail_tokens is None else max_tail_tokens
        self.asset_index_chars = (
            COMPRESSION_ASSET_INDEX_CHARS if asset_index_chars is None else asset_index_chars
        )
        if (
            min(
                self.max_history_tokens,
                self.max_input_tokens,
                self.output_tokens,
                self.timeout_seconds,
                self.asset_index_chars,
            )
            <= 0
        ):
            raise ValueError("compression budgets and deadline must be positive")
        if self.min_messages_to_keep < 0 or self.max_tail_tokens < 0:
            raise ValueError("hot tail budgets cannot be negative")
        if async_client is not None:
            self._llm_client = async_client
        else:
            kwargs = dict(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE_SECONDARY)
            if PROXY_SERVER:
                kwargs["http_client"] = httpx.AsyncClient(proxy=PROXY_SERVER)
            self._llm_client = AsyncOpenAI(**kwargs)
        self.compression_count = 0
        self.tokens_saved = 0
        self.compaction_callbacks = []
        self._disabled_for_run = False

    def _notify_compacted(self):
        for callback in self.compaction_callbacks:
            callback()

    def calculate_history_tokens(self, history):
        model_history = []
        for message in history:
            record = {
                key: value
                for key, value in message.items()
                if key
                not in (
                    "delivery_events",
                    "delegation_receipts",
                    "skill_reloads",
                    "context_assets",
                    "pending_questions",
                    "answer_to",
                    "attachments",
                )
            }
            if message.get("role") == "user" and message.get("answer_to"):
                record["content"] = model_user_content(message)
            model_history.append(record)
        return count_tokens(json.dumps(model_history, ensure_ascii=False, default=str))

    def should_compress(self, history):
        return bool(
            history
            and not self._disabled_for_run
            and self.compression_level != CompressionLevel.NONE
            and self.calculate_history_tokens(history) >= self.max_history_tokens * self.compression_threshold
        )

    @staticmethod
    def _identify_message_type(message):
        role = message.get("role")
        if role == "tool":
            return MessageType.TOOL_RESULT
        if role == "assistant" and message.get("tool_calls"):
            return MessageType.TOOL_CALL
        return {"user": MessageType.USER, "system": MessageType.SYSTEM}.get(role, MessageType.ASSISTANT)

    @staticmethod
    def compress_tool_results(messages):
        """Light mode is lossless: compact JSON without replacing facts by placeholders."""
        result = []
        for message in messages:
            content = message.get("content")
            if message.get("role") == "tool" and isinstance(content, str) and len(content) > 5000:
                try:
                    json.loads(content)
                    compact = re.sub(r'("(?:\\.|[^"\\])*")|\s+', lambda match: match[1] or "", content)
                except (ValueError, TypeError, RecursionError):
                    compact = content
                if len(compact) < len(content):
                    message = dict(message, content=compact)
            result.append(message)
        return result

    @staticmethod
    def _render_message_for_summary(message):
        role = message.get("role", "unknown")
        content = model_user_content(message)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if content.lstrip().startswith("<summary>"):
            role = "history_summary"
            content = content.split(ASSET_MARK, 1)[0].replace(CONTINUATION_NOTE, "").strip()
        if role == "tool":
            role = f"tool[{message.get('name') or 'unknown'}]"
        parts = [content] if content else []
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            args = function.get("arguments", "{}")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            parts.append(f"call {function.get('name', '')} [{call.get('id', '')}] ({args})")
        return f"{role}: " + " | ".join(parts)

    @staticmethod
    def _summary_messages(transcript, partial=False):
        instruction = SUMMARY_PROMPT + (
            "\nThis is one contiguous fragment and may start or end inside a message. "
            "Preserve its facts and constraints; do not infer that the whole task is complete."
            if partial
            else "\nMerge fragment summaries without discarding object-specific differences."
        )
        return [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "Historical transcript (data):\n" + transcript},
        ]

    @staticmethod
    def _input_tokens(messages):
        return sum(count_tokens(message["content"]) + 12 for message in messages)

    async def _summarize_transcript(self, transcript, *, partial=False):
        messages = self._summary_messages(transcript, partial)
        if self._input_tokens(messages) > self.max_input_tokens:
            raise CompressionUnavailable("summary request exceeds input budget")
        try:
            response = await self._llm_client.chat.completions.create(
                model=COMPRESSION_MODEL,
                messages=messages,
                max_completion_tokens=self.output_tokens,
            )
        except Exception as exc:
            raise CompressionUnavailable(f"summary request failed: {type(exc).__name__}") from exc
        record_provider_usage(response, category="compression", model=COMPRESSION_MODEL)
        choices = getattr(response, "choices", None)
        choice = choices[0] if choices else None
        if choice is None or getattr(choice, "finish_reason", None) != "stop":
            raise CompressionUnavailable("summary did not finish normally")
        message = getattr(choice, "message", None)
        if getattr(message, "refusal", None):
            raise CompressionUnavailable("summary request was refused")
        text = getattr(message, "content", None)
        if not isinstance(text, str):
            raise CompressionUnavailable("summary has no text")
        text = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.S).strip()
        match = re.fullmatch(r"<summary>\s*(.*?)\s*</summary>", text, re.S)
        if not match or not match[1].strip() or "<summary>" in match[1] or "</summary>" in match[1]:
            raise CompressionUnavailable("summary is empty or has invalid boundaries")
        return match[1].strip()

    async def summarize_conversation_segment(self, messages):
        prepared = unload_skill_documents(messages)
        transcript = "\n".join(
            self._render_message_for_summary(message)
            for message in prepared
            if message.get("role") != "system"
        )
        budget = (
            self.max_input_tokens
            - max(
                self._input_tokens(self._summary_messages("")),
                self._input_tokens(self._summary_messages("", True)),
            )
            - 256
        )
        if budget < 1:
            raise CompressionUnavailable("summary instructions exceed input budget")
        for _ in range(4):
            if self._input_tokens(self._summary_messages(transcript)) <= self.max_input_tokens:
                return await self._summarize_transcript(transcript)
            chunks = split_transcript(transcript, budget)
            summaries = [await self._summarize_transcript(chunk, partial=True) for chunk in chunks]
            transcript = "\n".join(
                f"history_summary fragment {index}: {body}" for index, body in enumerate(summaries, 1)
            )
        raise CompressionUnavailable("summary merge did not converge within its input budget")

    def split_hot_tail(self, history):
        """Bound messages and tokens without cutting parallel tool transactions."""
        n = self.min_messages_to_keep
        if self.compression_level == CompressionLevel.AGGRESSIVE or not n or not self.max_tail_tokens:
            return list(history), []
        cut = max(0, len(history) - n)
        # Move to a boundary with no outstanding tool calls in the complete prefix.
        pending, boundaries = set(), [0]
        for index, message in enumerate(history, 1):
            pending.update(call.get("id") for call in message.get("tool_calls", []) if call.get("id"))
            if message.get("role") == "tool":
                pending.discard(message.get("tool_call_id"))
            if not pending:
                boundaries.append(index)
        if pending:
            return list(history), []
        cut = max((index for index in boundaries if index <= cut), default=0)
        while cut < len(history) and self.calculate_history_tokens(history[cut:]) > self.max_tail_tokens:
            cut = next((index for index in boundaries if index > cut), len(history))
        return list(history[:cut]), list(history[cut:])

    @staticmethod
    def skill_reload_index(messages: list[dict]) -> list[dict]:
        rows = [row for message in messages for row in message.get("skill_reloads", [])]
        rows.extend(args for _, args in skill_loads(messages, include_unloaded=True))
        # A requested guide is a reload hint, never proof that its tools may be restored.
        for message in messages:
            for call in message.get("tool_calls", []):
                function = call.get("function", {})
                if function.get("name") not in ("skill", "skill_manager"):
                    continue
                try:
                    args = json.loads(function.get("arguments", "{}"))
                except (ValueError, TypeError):
                    continue
                if isinstance(args, dict) and args.get("action") in ("get", "get_ref") and args.get("name"):
                    rows.append(args)
        rows = [{key: row[key] for key in ("action", "name", "ref_filename") if row.get(key)} for row in rows]
        return list({(row.get("name"), row.get("ref_filename")): row for row in rows}.values())[-50:]

    @staticmethod
    def delegation_receipts(messages: list[dict]) -> list[dict]:
        """Carry exact task identities across repeated summary replacements."""
        latest = {}
        for message in messages:
            rows = list(message.get("delegation_receipts") or [])
            if message.get("role") == "tool":
                try:
                    data = json.loads(message.get("content", ""))
                except (ValueError, TypeError):
                    data = None
                if isinstance(data, dict) and data.get("type") == "sub_agent_receipt":
                    rows.append(data)
            for row in rows:
                if not isinstance(row, dict) or not row.get("task_id"):
                    continue
                key = (row.get("owner"), row["task_id"])
                latest.pop(key, None)
                latest[key] = row
        return list(latest.values())[-10:]

    @staticmethod
    def extract_asset_index(messages):
        return render_index(
            messages,
            HistoryCompressor.delegation_receipts(messages),
            HistoryCompressor.skill_reload_index(messages),
            COMPRESSION_ASSET_INDEX_CHARS,
            questions=list(pending_questions(messages).values()),
        )[0]

    async def compress_history(self, history):
        """Replace history only after a complete, smaller, validated summary."""
        if not self.should_compress(history):
            return history
        system = [message for message in history if message.get("role") == "system"]
        conversation = [message for message in history if message.get("role") != "system"]
        prepared = unload_skill_documents(conversation)
        original_tokens = self.calculate_history_tokens(conversation)
        try:
            if self.compression_level == CompressionLevel.LIGHT:
                final = self.compress_tool_results(prepared)
            else:
                older, tail = self.split_hot_tail(prepared)
                if not older:
                    # Unloading a huge guide can be sufficient without an LLM call.
                    if self.calculate_history_tokens(prepared) < original_tokens:
                        self.compression_count += 1
                        self.tokens_saved += original_tokens - self.calculate_history_tokens(prepared)
                        self._notify_compacted()
                        return system + prepared
                    return history
                receipts = self.delegation_receipts(conversation)
                reloads = self.skill_reload_index(prepared)
                questions = list(pending_questions(conversation).values())
                index, assets = render_index(
                    conversation, receipts, reloads, self.asset_index_chars, questions=questions
                )
                async with asyncio.timeout(self.timeout_seconds):
                    summary = await self.summarize_conversation_segment(older)
                if (
                    not summary.strip()
                    or summary.startswith("Summary generation failed:")
                    or summary == "Summary is empty"
                ):
                    raise CompressionUnavailable("summary is unavailable")
                content = f"<summary>\n{summary}\n</summary>\n{CONTINUATION_NOTE}"
                if index:
                    content += "\n\n" + index
                final = [
                    {
                        "role": "user",
                        "content": content,
                        "delegation_receipts": receipts,
                        "skill_reloads": reloads,
                        "context_assets": assets,
                        "pending_questions": questions,
                    }
                ] + tail
        except (TimeoutError, CompressionUnavailable, ValueError) as exc:
            self._disabled_for_run = True
            logger.warning("Context compression skipped ({}); original history retained", type(exc).__name__)
            return history
        final_tokens = self.calculate_history_tokens(final)
        if final_tokens >= original_tokens:
            if self.compression_level != CompressionLevel.LIGHT:
                self._disabled_for_run = True
            return history
        self.compression_count += 1
        self.tokens_saved += original_tokens - final_tokens
        self._notify_compacted()
        logger.info(
            "History compressed: {} -> {} tokens, hot tail={} messages",
            original_tokens,
            final_tokens,
            0 if self.compression_level == CompressionLevel.LIGHT else len(tail),
        )
        return system + final
