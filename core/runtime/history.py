"""SDK serialization and saved history normalization, without agent dependencies."""

import json
import re
from uuid import uuid4

from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

from core.runtime.questions import model_user_content
from utils.tool_names import canonical_tool_name


def _history_to_sdk_input(history: list[dict]) -> list[dict]:
    """Restore tool pairs as Responses items, retaining guides and task receipts."""
    messages = []
    output_ids = {msg.get("tool_call_id") for msg in history if msg.get("role") == "tool"}
    pending, seen = {}, set()
    for msg in history:
        role = msg.get("role", "")
        if role == "system":
            continue
        if role == "tool":
            call_id = pending.pop(msg.get("tool_call_id"), None)
            if call_id:
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(msg.get("content") or ""),
                    }
                )
            continue
        content = model_user_content(msg)
        if content is None:
            content = ""
        if str(content).strip() or role != "assistant":
            messages.append({"role": role, "content": str(content)})
        for call in msg.get("tool_calls", []):
            original_id = call.get("id")
            if not original_id or original_id not in output_ids:
                continue
            call_id = original_id if original_id not in seen else f"history_{len(messages)}_{uuid4().hex[:8]}"
            seen.add(call_id)
            pending[original_id] = call_id
            function = call.get("function", {})
            arguments = function.get("arguments", "{}")
            messages.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": canonical_tool_name(function.get("name", "")),
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                }
            )
    return messages


def _sdk_result_to_history(prev_history: list[dict], result) -> list[dict]:
    """Extract conversation history from SDK run result.

    Appends the new messages from the SDK run to the previous history.
    """
    history = list(prev_history)
    call_names = {
        call.get("id"): call.get("function", {}).get("name", "")
        for msg in history
        for call in msg.get("tool_calls", [])
    }
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
                history.append({"role": "assistant", "content": text, "message_id": getattr(raw, "id", "")})
        elif isinstance(item, ToolCallItem):
            raw = item.raw_item
            call_names[getattr(raw, "call_id", "")] = canonical_tool_name(getattr(raw, "name", ""))
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
                    "name": call_names.get(tc_id, ""),
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
        if "name" in clean:
            clean["name"] = canonical_tool_name(clean["name"])
        if clean.get("tool_calls"):
            clean["tool_calls"] = [
                {
                    **call,
                    "function": {
                        **call.get("function", {}),
                        "name": canonical_tool_name(call.get("function", {}).get("name", "")),
                    },
                }
                for call in clean["tool_calls"]
            ]
        cleaned.append(clean)
    return cleaned


def interrupted_output(name, details=None):
    payload = {
        "status": "interrupted",
        "outcome": "unknown",
        "tool": name,
        "instruction": "Local waiting stopped; the remote operation may have happened. Verify its outcome before retrying.",
    }
    if details:
        payload["delegations"] = [
            {key: value for key, value in detail.items() if key not in ("delivery_events", "request")}
            for detail in details
        ]
    return json.dumps(payload, ensure_ascii=False)
