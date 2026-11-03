"""Compose SDK streaming, compaction and interruption-safe execution facts."""

from contextlib import aclosing
from uuid import uuid4

from agents import RunHooks
from agents.stream_events import RawResponsesStreamEvent

from core.context_compaction import SDKContextCompaction
from core.runtime.events import _stream_sdk_events
from core.runtime.history import interrupted_output
from core.runtime.usage import record_sdk_usage
from utils.usage import current_usage


def _identities(history):
    calls = {call.get("id") for message in history for call in message.get("tool_calls", [])}
    outputs = {message.get("tool_call_id") for message in history if message.get("role") == "tool"}
    messages = {message.get("message_id") for message in history if message.get("message_id")}
    return calls, outputs, messages


class ExecutionJournal(RunHooks):
    """Record individual tool completions even while sibling calls are pending."""

    def __init__(self):
        self.calls, self.outputs, self.messages, self.partial = {}, {}, {}, {}
        self.retired_calls, self.retired_messages = set(), set()

    def _call(self, raw):
        call_id = getattr(raw, "call_id", "")
        if call_id and call_id not in self.retired_calls:
            self.calls[call_id] = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": getattr(raw, "name", ""),
                    "arguments": getattr(raw, "arguments", "{}"),
                },
            }

    async def on_tool_start(self, context, agent, tool):
        self._call(getattr(context, "tool_call", None))

    async def on_tool_end(self, context, agent, tool, result):
        self._call(getattr(context, "tool_call", None))
        call_id = getattr(context, "tool_call_id", "")
        if call_id and call_id not in self.retired_calls:
            self.outputs[call_id] = {
                "role": "tool",
                "name": tool.name,
                "tool_call_id": call_id,
                "content": str(result),
                "execution_status": "completed",
            }

    def observe(self, event):
        if not isinstance(event, RawResponsesStreamEvent):
            return
        data = event.data
        if getattr(data, "type", "") == "response.output_text.delta":
            message_id = getattr(data, "item_id", "") or "partial-response"
            if message_id not in self.retired_messages:
                self.partial.setdefault(message_id, []).append(getattr(data, "delta", ""))
        if getattr(data, "type", "") == "response.completed":
            for raw in getattr(data.response, "output", []):
                if getattr(raw, "type", "") == "function_call":
                    self._call(raw)
                elif getattr(raw, "type", "") == "message":
                    message_id = getattr(raw, "id", "")
                    if message_id and message_id not in self.retired_messages:
                        text = "".join(getattr(part, "text", "") for part in getattr(raw, "content", []))
                        if text:
                            self.messages[message_id] = {
                                "role": "assistant",
                                "content": text,
                                "message_id": message_id,
                            }
                        self.partial.pop(message_id, None)

    def checkpoint(self, history):
        calls, _, messages = _identities(history)
        self.retired_calls.update(calls)
        self.retired_messages.update(messages)
        self.calls.clear()
        self.outputs.clear()
        self.messages.clear()
        self.partial.clear()

    def merge(self, history, *, interrupted=False, tools=()):
        history = list(history)
        calls, outputs, messages = _identities(history)
        history.extend(message for key, message in self.messages.items() if key not in messages)
        for call_id, call in self.calls.items():
            if call_id not in calls:
                history.append({"role": "assistant", "content": "", "tool_calls": [call]})
        history.extend(message for key, message in self.outputs.items() if key not in outputs)
        if interrupted:
            calls, outputs, messages = _identities(history)
            for message in list(history):
                for call in message.get("tool_calls", []):
                    if call.get("id") in outputs:
                        continue
                    details = []
                    for tool in tools:
                        snapshot = getattr(tool, "interruption_snapshot", None)
                        if tool.name == call.get("function", {}).get("name") and snapshot:
                            details = snapshot(call.get("function", {}).get("arguments", "{}"))
                    history.append(
                        {
                            "role": "tool",
                            "name": call.get("function", {}).get("name", ""),
                            "tool_call_id": call.get("id"),
                            "execution_status": "interrupted",
                            "content": interrupted_output(call.get("function", {}).get("name", ""), details),
                            "delivery_events": [
                                event for detail in details for event in detail.get("delivery_events", [])
                            ],
                        }
                    )
                    outputs.add(call.get("id"))
            for message_id, parts in self.partial.items():
                if message_id not in messages and parts:
                    history.append(
                        {
                            "role": "assistant",
                            "message_id": message_id,
                            "partial": True,
                            "content": "[Incomplete assistant response generated before interruption]\n"
                            + "".join(parts),
                        }
                    )
        return history


class SDKRunSession:
    """One run's journal, event channel and compaction are composed here."""

    def __init__(
        self,
        agent,
        compressor,
        history,
        tools,
        *,
        on_history=None,
        event_stream=_stream_sdk_events,
        usage_category="main",
    ):
        self.journal = ExecutionJournal()
        self.tools, self.event_stream = tools, event_stream
        self.compaction = SDKContextCompaction(
            agent, compressor, history, tools, on_history=on_history, on_compacted=self.journal.checkpoint
        )
        self.result = None
        self.interrupted = False
        self.usage_collector = current_usage()
        self.usage_category = usage_category
        self.run_id = uuid4().hex
        self.model = agent.model if isinstance(agent.model, str) else type(agent.model).__name__
        self.response_models = {}
        self.response_count = 0

    def observe(self, event):
        self.journal.observe(event)
        if (
            isinstance(event, RawResponsesStreamEvent)
            and getattr(event.data, "type", "") == "response.completed"
        ):
            response = event.data.response
            index = self.response_count
            self.response_count += 1
            if getattr(response, "model", None):
                self.response_models[index] = response.model
            if self.usage_collector:
                self.usage_collector.add(
                    getattr(response, "model", None) or self.model,
                    getattr(response, "usage", None),
                    category=self.usage_category,
                    key=(self.run_id, index),
                )

    def start(self, run_streamed, agent, *, input, max_turns):
        self.result = run_streamed(agent, input=input, max_turns=max_turns, hooks=self.journal)
        self.compaction.bind(self.result)

    async def stream(self):
        try:
            async with aclosing(self.event_stream(self.result, self.tools, observer=self.observe)) as events:
                async for event in events:
                    yield event
        except BaseException:
            self.interrupted = True
            raise
        finally:
            record_sdk_usage(
                self.usage_collector,
                self.result,
                run_id=self.run_id,
                model=self.model,
                category=self.usage_category,
                response_models=self.response_models,
            )

    def snapshot(self):
        return self.journal.merge(self.compaction.snapshot(), interrupted=self.interrupted, tools=self.tools)
