"""Identify successfully loaded skill documents without trusting prose summaries."""

import json


def skill_loads(history, *, include_unloaded=False):
    """Yield (result message, arguments) for intact skill get/get_ref pairs."""
    calls = {}
    for message in history:
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            if function.get("name") not in ("skill", "skill_manager"):
                continue
            try:
                args = json.loads(function.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            if isinstance(args, dict) and args.get("action") in ("get", "get_ref") and args.get("name"):
                calls[call.get("id")] = args
        if message.get("role") != "tool" or (message.get("skill_unloaded") and not include_unloaded):
            continue
        args = calls.pop(message.get("tool_call_id"), None)
        if args is None:
            continue
        body = str(message.get("content", ""))
        name = args["name"]
        if (
            (include_unloaded and message.get("skill_unloaded"))
            or f"Skill document `{name}/SKILL.md`" in body
            or (args["action"] == "get_ref" and f"`skills/{name}/references/" in body)
        ):
            yield message, args


def loaded_skill_names(history):
    return list(
        dict.fromkeys(
            args["name"]
            for message, args in skill_loads(history)
            if f"Skill document `{args['name']}/SKILL.md`" in str(message.get("content", ""))
        )
    )


def unload_skill_documents(history):
    """Replace original guides before any truncation or LLM summarization."""
    loads = {id(message): args for message, args in skill_loads(history)}
    result = []
    for message in history:
        if id(message) in loads:
            args = loads[id(message)]
            message = dict(
                message,
                skill_unloaded=True,
                content=(
                    "[Skill instructions unloaded by context compression. "
                    f"Reload the original guide with skill(action='get', name={args['name']!r})"
                    + (
                        f", then skill(action='get_ref', name={args['name']!r}, "
                        f"ref_filename={args.get('ref_filename', '')!r})"
                        if args["action"] == "get_ref"
                        else ""
                    )
                    + ". Do not execute this workflow from a summary.]"
                ),
            )
        result.append(message)
    return result
