"""Step-boundary SDK compaction without restarting runs or replaying tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agents import Agent

from core.runtime.events import _attach_delivery_events
from core.runtime.history import _history_to_sdk_input, _sdk_result_to_history


@dataclass
class CompactingAgent(Agent):
    context_compaction: SDKContextCompaction | None = field(default=None, repr=False, compare=False)

    async def get_all_tools(self, run_context):
        # The SDK calls this after the preceding parallel tool batch completes and
        # before taking a fresh snapshot of schemas for the next model request.
        if self.context_compaction:
            await self.context_compaction.before_model()
        return await super().get_all_tools(run_context)


class SDKContextCompaction:
    def __init__(self, agent, compressor, history, tools, on_history=None, on_compacted=None):
        self.compressor = compressor
        self.history = list(history)
        self.tools = tools
        self.result = None
        self.on_history = on_history
        self.on_compacted = on_compacted
        agent.context_compaction = self

    def bind(self, result):
        """Bind synchronously before the SDK's background task can run."""
        self.result = result

    def snapshot(self):
        history = _sdk_result_to_history(self.history, self.result) if self.result else list(self.history)
        _attach_delivery_events(history, self.tools)
        return history

    async def before_model(self):
        if self.result is None:
            return
        current = self.snapshot()
        if not self.compressor.should_compress(current):
            return
        if (
            self.compressor.compression_count
            and self.compressor.calculate_history_tokens(current) < self.compressor.max_history_tokens
        ):
            # A reloaded guide may cross the soft watermark. Give the next model
            # request a chance to use it while still below the full history budget.
            # Otherwise a large guide can be loaded and immediately removed forever.
            calls = [
                call
                for message in _sdk_result_to_history([], self.result)
                for call in message.get("tool_calls", [])
            ]
            if calls and all(self._is_guide_load(call) for call in calls):
                return
        compressed = await self.compressor.compress_history(current)
        if compressed is current:
            return
        if self.on_compacted:
            self.on_compacted(current)
        self.history = compressed
        self.result.input = _history_to_sdk_input(compressed)
        # These completed items are now represented in history. Their queued SSE
        # events and raw responses remain intact, but must not be appended twice.
        self.result.new_items = []
        if self.on_history:
            self.on_history(compressed)

    @staticmethod
    def _is_guide_load(call):
        function = call.get("function", {})
        if function.get("name") not in ("skill", "skill_manager"):
            return False
        try:
            args = json.loads(function.get("arguments", "{}"))
        except (ValueError, TypeError):
            return False
        return isinstance(args, dict) and args.get("action") in ("get", "get_ref")
