"""Reference paths and discoverable links for multi-file skills."""

import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import quote

# Size limits applied when a change is proposed. They bound what is staged for
# confirmation, shown to the user and sent through the event stream; they are
# deliberately not part of normalize_references so existing skills keep loading.
MAX_INSTRUCTIONS_BYTES = 30 * 1024
MAX_REFERENCE_BYTES = 64 * 1024
MAX_REFERENCES_TOTAL_BYTES = 256 * 1024
MAX_REFERENCE_COUNT = 20


def normalize_reference_path(path: str) -> str:
    path = path.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or any(ord(char) < 32 for char in path):
        raise ValueError("References must use relative paths within the references directory")
    if path.startswith("references/"):
        path = path[len("references/") :]
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("Reference paths cannot contain empty, dot or parent segments")
    return path


def normalize_references(references: dict[str, str]) -> dict[str, str]:
    result = {}
    for filename, content in references.items():
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Reference bodies must be nonempty strings")
        path = normalize_reference_path(filename)
        if path in result:
            raise ValueError(f"Duplicate reference path: {path}")
        result[path] = content
    return result


def with_reference_links(instructions: str, references: dict[str, str]) -> str:
    missing = [
        path
        for path in references
        if f"references/{path}" not in instructions
        and f"references/{quote(path, safe='/')}" not in instructions
    ]
    if not missing:
        return instructions
    links = []
    for path in missing:
        label = path.replace("[", "\\[").replace("]", "\\]")
        links.append(f"- [{label}](references/{quote(path, safe='/')})")
    return (
        instructions.rstrip()
        + '\n\n## Reference files\n\nRead on demand with `skill(action="get_ref", ...)`:\n'
        + "\n".join(links)
    )


def _collision_key(path: str) -> str:
    """Names that differ only by case or Unicode form collide on common filesystems."""
    return unicodedata.normalize("NFC", path).casefold()


def validate_reference_set(
    references: dict[str, str],
    *,
    instructions: str = "",
    existing_paths: Iterable[str] = (),
) -> dict[str, str]:
    """Normalize a proposed reference set and enforce size and collision rules.

    ``existing_paths`` are the references already stored for the target skill: a new
    name may not differ from one of them only by case or Unicode form, while an
    identical name is an intentional replacement.
    """
    result = {}
    for path, content in normalize_references(references).items():
        result[unicodedata.normalize("NFC", path)] = content
    if len(result) != len(references):
        raise ValueError("Duplicate reference path after Unicode normalization")
    taken = {}
    for path in result:
        key = _collision_key(path)
        if key in taken:
            raise ValueError(f"References {taken[key]!r} and {path!r} differ only by case or Unicode form")
        taken[key] = path
    existing = {_collision_key(path): path for path in existing_paths}
    for path in result:
        other = existing.get(_collision_key(path))
        if other is not None and other != path:
            raise ValueError(f"Reference {path!r} collides with existing {other!r}; use the existing name")
    if len(result) > MAX_REFERENCE_COUNT:
        raise ValueError(f"A proposal may contain at most {MAX_REFERENCE_COUNT} references")
    total = 0
    for path, content in result.items():
        size = len(content.encode("utf-8"))
        if size > MAX_REFERENCE_BYTES:
            raise ValueError(f"Reference {path!r} exceeds {MAX_REFERENCE_BYTES // 1024}KB; split it by topic")
        total += size
    if total > MAX_REFERENCES_TOTAL_BYTES:
        raise ValueError(f"References exceed {MAX_REFERENCES_TOTAL_BYTES // 1024}KB in total")
    if len(instructions.encode("utf-8")) > MAX_INSTRUCTIONS_BYTES:
        raise ValueError(
            f"Instructions exceed {MAX_INSTRUCTIONS_BYTES // 1024}KB; move detail into references"
        )
    return result
