"""History compressor - prevents context bloat during long conversations.

Ported from agentplayground's HistoryCompressor, adapted for EvoAgent
(uses AsyncOpenAI instead of agentic_go.LLM).

Three compression levels:
- LIGHT:      Compress large tool outputs only
- MODERATE:   Compress tool outputs + LLM-summarize old message segments
- AGGRESSIVE: Single summary + essential messages only
"""

import json
import re
from enum import Enum

import httpx
from openai import AsyncOpenAI

from configs.settings import (
    COMPRESSION_MODEL,
    COMPRESSION_THRESHOLD,
    MAX_HISTORY_TOKENS,
    MIN_MESSAGES_TO_KEEP,
    OPENAI_API_BASE_SECONDARY,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from utils.logger import logger
from utils.token_counter import count_tokens


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


class HistoryCompressor:
    """History compression manager for preventing context bloat."""

    def __init__(
        self,
        max_history_tokens: int = None,
        compression_threshold: float = None,
        min_messages_to_keep: int = None,
        compression_level: str = "moderate",
        async_client: AsyncOpenAI | None = None,
    ):
        self.max_history_tokens = max_history_tokens or MAX_HISTORY_TOKENS
        self.compression_threshold = compression_threshold or COMPRESSION_THRESHOLD
        self.min_messages_to_keep = min_messages_to_keep or MIN_MESSAGES_TO_KEEP
        self.compression_level = CompressionLevel(compression_level)

        if async_client is not None:
            self._llm_client = async_client
        elif PROXY_SERVER:
            self._llm_client = AsyncOpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE_SECONDARY,
                http_client=httpx.AsyncClient(proxy=PROXY_SERVER),
            )
        else:
            self._llm_client = AsyncOpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE_SECONDARY,
            )

        self.compression_count = 0
        self.tokens_saved = 0

    def calculate_history_tokens(self, history: list[dict]) -> int:
        """Calculate total tokens in history."""
        try:
            return count_tokens(json.dumps(history, default=str))
        except Exception:
            return len(str(history))

    def should_compress(self, history: list[dict]) -> bool:
        """Check if compression is needed."""
        if not history:
            return False
        current_tokens = self.calculate_history_tokens(history)
        threshold_tokens = self.max_history_tokens * self.compression_threshold
        return current_tokens >= threshold_tokens

    @staticmethod
    def _identify_message_type(message: dict) -> MessageType:
        role = message.get("role")
        if role == "user":
            return MessageType.USER
        elif role == "assistant":
            if message.get("tool_calls"):
                return MessageType.TOOL_CALL
            return MessageType.ASSISTANT
        elif role == "tool":
            return MessageType.TOOL_RESULT
        elif role == "system":
            return MessageType.SYSTEM
        return MessageType.ASSISTANT

    # --- Compression strategies ---

    def compress_tool_results(self, messages: list[dict]) -> list[dict]:
        """Compress large tool outputs (> 5000 chars)."""
        compressed = []
        for msg in messages:
            msg = msg.copy()
            if self._identify_message_type(msg) == MessageType.TOOL_RESULT:
                content = str(msg.get("content", ""))
                if len(content) > 5000:
                    urls = self._extract_urls(content)
                    if urls:
                        msg["content"] = f"[Compressed] Tool result links: {urls}"
                    else:
                        msg["content"] = (
                            f"[Compressed] Tool output (original length: {len(content)} characters)"
                        )
            compressed.append(msg)
        return compressed

    async def summarize_conversation_segment(self, messages: list[dict]) -> str:
        """Summarize conversation for continuation (recent detail + historical brevity)."""
        lines = []
        total_chars = 0
        max_total_chars = 30000

        for msg in messages:
            content = msg.get("content")
            if content is None:
                content = "[Tool call]"
            else:
                content = str(content)[:800]
            line = f"{msg.get('role', 'unknown')}: {content}"
            if total_chars + len(line) > max_total_chars:
                lines.append(f"... (omitted {len(messages) - len(lines)} remaining messages)")
                break
            lines.append(line)
            total_chars += len(line)

        conversation_text = "\n".join(lines)
        prompt = (
            "Create a continuation summary that will completely replace this conversation history. "
            "The agent must be able to resume using only your summary.\n\n"
            f"{conversation_text}\n\n"
            "Wrap the summary in <summary></summary> and include these nine sections:\n\n"
            "## 1. Primary Request and Intent\n"
            "The original request and subsequent steering. Quote critical requirements.\n\n"
            "## 2. Key Technical Concepts\n"
            "Technical concepts, decisions and their reasons.\n\n"
            "## 3. Files and Code Sections\n"
            "File paths, their purposes, exact changes and essential signatures or configuration values.\n\n"
            "## 4. Errors and Fixes\n"
            "Errors, resolutions, unsuccessful approaches to avoid repeating, and exact user corrections.\n\n"
            "## 5. Problem Solving\n"
            "Resolved problems and ongoing investigations with their current progress.\n\n"
            "## 6. All User Messages\n"
            "Key points from every non-tool user message, preserving the original phrasing.\n\n"
            "## 7. Pending Tasks\n"
            "Requested tasks that have not started.\n\n"
            "## 8. Current Work\n"
            "The interrupted task, exact state and intermediate results.\n\n"
            "## 9. Optional Next Steps\n"
            "Suggested next actions.\n\n"
            "Preserve exact URLs, paths, IDs, names, error messages, user corrections, negative feedback, "
            "values, formulas, configuration and technical constraints. "
            "If space is limited, prioritize corrections, errors, ongoing work, then completed work. "
            "Omit pleasantries, acknowledgements and repeated explanations. "
            "Give recent messages greater weight and emphasize what to do next."
        )

        try:
            response = await self._llm_client.chat.completions.create(
                model=COMPRESSION_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Summarize conversations for continuation. Your summary replaces the original history and must let the agent resume seamlessly.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_completion_tokens=16000,
            )
            result = response.choices[0].message.content
            # Some models (e.g. DeepSeek-R1) may put content in reasoning_content
            if result is None:
                result = getattr(response.choices[0].message, "reasoning_content", None)
            return result or "Summary is empty"
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return f"Summary generation failed: {str(e)}"

    # --- Split helpers ---

    def _find_safe_split_index(self, history: list[dict], min_keep: int) -> int:
        """Find a safe split index based on turn boundaries.

        Ensures recent messages always start at a new user turn.
        """
        if len(history) <= min_keep:
            return 0

        # Find all turn starts (user messages)
        turn_starts = [
            i for i, msg in enumerate(history) if self._identify_message_type(msg) == MessageType.USER
        ]

        if not turn_starts:
            return 0

        min_start = len(history) - min_keep
        for idx in turn_starts:
            if idx >= min_start:
                return idx

        return turn_starts[-1]

    def _extract_essential_messages(self, messages: list[dict]) -> list[dict]:
        """Extract only essential messages (user queries, substantial responses)."""
        essential = []
        for msg in messages:
            msg_type = self._identify_message_type(msg)
            if msg_type == MessageType.USER:
                essential.append(msg)
            elif msg_type == MessageType.ASSISTANT and not msg.get("tool_calls"):
                content = msg.get("content", "")
                if len(content) > 100:
                    essential.append(msg)
        return essential

    @staticmethod
    def _extract_urls(content: str) -> str:
        urls = re.findall(r"https?://[^\s\)]+", content)
        return ", ".join(urls[:10]) if urls else ""

    @staticmethod
    def extract_asset_index(messages: list[dict]) -> str:
        """Extract structured asset index from history messages.

        Each asset is tagged with its conversation turn number for temporal context.
        """
        skills: set = set()
        references: set = set()
        user_images: list = []  # [(turn, url)]
        fetched_urls: list = []  # [(turn, url)]
        generated_images: list = []  # [(turn, url)]
        turn = 0

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                turn += 1

            # User-uploaded images
            if role == "user" and "[User-uploaded images]" in content:
                after = content.split("[User-uploaded images]")[-1]
                for url in re.findall(r"https?://\S+", after):
                    user_images.append((turn, url))

            # Tool calls: fetchurl URLs, skill get/get_ref (including legacy names)
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    fname = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if fname == "fetchurl" and args.get("url"):
                        fetched_urls.append((turn, args["url"]))
                    elif fname in ("skill", "skill_manager"):
                        action = args.get("action", "")
                        if action == "get" and args.get("name"):
                            skills.add(args["name"])
                        elif action == "get_ref" and args.get("ref_filename"):
                            references.add(args["ref_filename"])

            # Tool results: image generation output
            if role == "tool":
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        for img in data.get("images", []):
                            if isinstance(img, dict) and img.get("image_path"):
                                generated_images.append((turn, img["image_path"]))
                        if data.get("image_path") and "images" not in data:
                            generated_images.append((turn, data["image_path"]))
                except (json.JSONDecodeError, TypeError):
                    pass

        parts = []
        if skills:
            parts.append(f"- Active skills:{', '.join(sorted(skills))}")
        if references:
            parts.append(f"- loadedReference files:{', '.join(sorted(references))}")
        if user_images:
            seen = {}
            for t, url in user_images:
                if url not in seen:
                    seen[url] = t
            parts.append("- User images:" + ", ".join(f"{url}(turn {t})" for url, t in seen.items()))
        if fetched_urls:
            seen = {}
            for t, url in fetched_urls:
                if url not in seen:
                    seen[url] = t
            parts.append("- Fetched URLs:" + ", ".join(f"{url}(turn {t})" for url, t in seen.items()))
        if generated_images:
            seen = {}
            for t, url in generated_images:
                if url not in seen:
                    seen[url] = t
            parts.append(
                f"- Generated images ({len(seen)} total):"
                + ", ".join(f"{url}(turn {t})" for url, t in seen.items())
            )

        return "[Session asset index]\n" + "\n".join(parts) if parts else ""

    # --- Main compression ---

    async def compress_history(self, history: list[dict]) -> list[dict]:
        """Compress conversation history based on the configured compression level.

        Separates system prompt, compresses older messages, keeps recent ones intact.
        Summary messages use role=assistant so they survive _sanitize_history on reload.
        Returns the compressed history list.
        """
        if not self.should_compress(history):
            return history

        # Separate system prompt from conversation
        system_msg = None
        conversation = history
        if history and history[0].get("role") == "system":
            system_msg = history[0]
            conversation = history[1:]

        if not conversation:
            return history

        original_tokens = self.calculate_history_tokens(conversation)

        # Extract asset index from ALL messages before compression
        asset_index = self.extract_asset_index(conversation)

        # Split at turn boundary
        split_idx = self._find_safe_split_index(conversation, self.min_messages_to_keep)
        older_messages = conversation[:split_idx]
        recent_messages = conversation[split_idx:]

        # Always compress large tool results in recent messages
        recent_messages = self.compress_tool_results(recent_messages)

        if not older_messages:
            final = recent_messages
            final_tokens = self.calculate_history_tokens(final)
            if final_tokens < original_tokens:
                if system_msg:
                    final = [system_msg] + final
                self.compression_count += 1
                self.tokens_saved += original_tokens - final_tokens
                logger.info(
                    f"History compressed (tool results only): {original_tokens} → {final_tokens} tokens "
                    f"({100 * (1 - final_tokens / (original_tokens + 1)):.1f}% reduction)"
                )
                return final
            return history

        if self.compression_level == CompressionLevel.LIGHT:
            # LIGHT: only compress tool outputs, keep structure
            final = self.compress_tool_results(older_messages) + recent_messages

        elif self.compression_level in (CompressionLevel.MODERATE, CompressionLevel.AGGRESSIVE):
            # MODERATE/AGGRESSIVE: full replacement — summary + asset index
            compressed_tool = self.compress_tool_results(conversation)
            summary = await self.summarize_conversation_segment(compressed_tool)

            summary_content = f"<summary>\n{summary}\n</summary>"
            if asset_index:
                summary_content += f"\n\n{asset_index}"

            final = [
                {
                    "role": "user",
                    "content": summary_content,
                }
            ]

        else:
            final = older_messages + recent_messages

        # Calculate tokens for fair comparison
        final_tokens = self.calculate_history_tokens(final)
        if system_msg:
            final = [system_msg] + final
        self.compression_count += 1
        self.tokens_saved += original_tokens - final_tokens

        logger.info(
            f"History compressed: {original_tokens} → {final_tokens} tokens "
            f"({100 * (1 - final_tokens / (original_tokens + 1)):.1f}% reduction)"
        )
        return final
