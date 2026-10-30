"""Shared context limits and workspace paths exposed to agents."""

import json

MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_BYTES = 25 * 1024


def memory_index_notice(body: str) -> str:
    """Report index pressure without deleting persisted memories."""
    lines = len(body.splitlines())
    size = len(body.encode("utf-8"))
    if lines > MEMORY_INDEX_MAX_LINES or size > MEMORY_INDEX_MAX_BYTES:
        return (
            f"Memory index exceeds the loading limit ({lines} lines, {size} bytes; limit: 200 lines or 25KB). "
            "The full file is preserved, but excess content is not automatically loaded. "
            "Merge related entries, remove expired entries and shorten summaries using rewrite. "
            "Keep details in body files. Use read to view the full index."
        )
    if lines >= MEMORY_INDEX_MAX_LINES * 0.9 or size >= MEMORY_INDEX_MAX_BYTES * 0.9:
        return "Memory index is near the 200-line or 25KB limit. Merge duplicate or expired entries and shorten summaries."
    return ""


def memory_index_context(body: str) -> str:
    """Load only the index prefix within both limits, preserving UTF-8."""
    prefix = "\n".join(body.splitlines()[:MEMORY_INDEX_MAX_LINES])
    prefix = prefix.encode("utf-8")[:MEMORY_INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
    notice = memory_index_notice(body)
    return prefix + (f"\n\n[Index maintenance] {notice}" if notice else "")


def skill_location_context(name: str) -> str:
    """Use a workspace-relative path valid for local and Docker execution."""
    directory = f"skills/{name}"
    cwd = json.dumps(directory, ensure_ascii=False)
    return (
        f"Skill directory relative to the user workspace: `{directory}/`\n"
        f"Skill file: `{directory}/SKILL.md`\n"
        "Resolve scripts/, references/ and other relative paths from the skill directory.\n"
        f'Run an existing helper with execute_code using cwd={cwd}, for example code="python scripts/run.py".\n'
        "Docker mounts the workspace at /workspace; local execution uses the same relative cwd."
    )
