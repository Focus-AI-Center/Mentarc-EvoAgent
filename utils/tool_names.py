"""Compatibility for persisted skills and tool history created before renaming."""

import re

_LEGACY_TOOL_NAMES = {"update_memory": "memory", "skill_manager": "skill"}
_LEGACY_TOOL_RE = re.compile(r"(?<![\w./\\-])(update_memory|skill_manager)(?![\w./\\-])")


def canonical_tool_name(name: str) -> str:
    return _LEGACY_TOOL_NAMES.get(name, name)


def normalize_tool_references(text: str) -> str:
    return _LEGACY_TOOL_RE.sub(lambda match: canonical_tool_name(match.group()), text)
