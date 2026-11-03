"""SDK-to-SSE translation and request-local delegated event channels."""

import asyncio
import json
from collections.abc import AsyncGenerator

from agents.items import ToolCallItem, ToolCallOutputItem

from utils.tool_results import tool_observation


def _find_sub_agent_tool(tools: list):
    """Find the shared delegation entry point."""
    for t in tools:
        if getattr(t, "name", "") == "sub_agent":
            return t
    return None


def _attach_delivery_events(history, tools):
    """UI replay metadata stays outside the model-visible receipt content."""
    tool = _find_sub_agent_tool(tools)
    if tool is None:
        return
    for message in history:
        if message.get("role") != "tool" or message.get("name") != "sub_agent":
            continue
        try:
            receipt = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(receipt, dict) and receipt.get("delivery_id") in tool.deliveries:
            message["delivery_events"] = tool.deliveries[receipt["delivery_id"]]


async def _stream_sdk_events(result, tools: list, *, observer=None) -> AsyncGenerator[dict, None]:
    """Multiplex SDK output and live child events without invocation buffers."""
    queue = asyncio.Queue()
    done = object()
    bindings = [(tool, tool.event_sink) for tool in tools if hasattr(tool, "event_sink")]
    for tool, _ in bindings:
        tool.event_sink = queue.put

    async def pump():
        try:
            async for event in _map_sdk_events(result, observer=observer):
                await queue.put(event)
        finally:
            await queue.put(done)

    producer = asyncio.create_task(pump())
    try:
        while (event := await queue.get()) is not done:
            yield event
        await producer
    finally:
        if hasattr(result, "cancel") and not getattr(result, "is_complete", False):
            result.cancel()
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        for tool, previous in bindings:
            tool.event_sink = previous


async def _map_sdk_events(result, *, observer=None) -> AsyncGenerator[dict, None]:
    """Stream events from OpenAI Agents SDK run and yield SSE-compatible dicts.

    Maps SDK events to existing SSE codes for frontend compatibility:
      code 2: text delta
      code 4: tool_call
      code 5: tool_result
      code 7: schedule_manager
      code 8: html_render
      code 9: todo_manager progress
      code 10: ask_user_question
      code 12: complete sub-agent skill proposals for user confirmation
    """
    from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent

    # Cache call_id → tool_name for matching tool results to their calls
    _call_id_to_name: dict[str, str] = {}

    async for event in result.stream_events():
        if observer:
            observer(event)
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
                observation = tool_observation(output_str)

                # Detect html_render
                if tc_name == "html_render" and not observation["is_error"]:
                    yield {
                        "code": 5,
                        "content": json.dumps(
                            {
                                "id": tc_id,
                                "name": tc_name,
                                **observation,
                                "result": "HTML page generated; rendering preview...",
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
                                **observation,
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
