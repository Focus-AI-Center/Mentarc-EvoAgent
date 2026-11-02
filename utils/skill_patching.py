"""Exact text edits for skill instructions and reference documents."""

import re

# Keep the frontmatter and all body whitespace byte-for-byte outside the edit.
_FRONTMATTER_PREFIX = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)


def patch_skill_text(content: str, old_string: str, new_string: str, *, main_document: bool = False) -> str:
    """Replace one unambiguous literal occurrence, preserving main-document metadata."""
    if not old_string:
        raise ValueError("patch requires a nonempty old_string.")
    if old_string == new_string:
        raise ValueError("new_string must differ from old_string.")

    start = 0
    if main_document:
        frontmatter = _FRONTMATTER_PREFIX.match(content)
        if frontmatter:
            start = frontmatter.end()
    body = content[start:]
    index = body.find(old_string)
    if index < 0:
        raise ValueError("old_string was not found. Reload the target and copy an exact current fragment.")
    # Check overlapping occurrences as well, such as 'aa' inside 'aaa'.
    if body.find(old_string, index + 1) >= 0:
        raise ValueError("old_string matches more than once. Include more surrounding text.")
    return content[:start] + body[:index] + new_string + body[index + len(old_string) :]
