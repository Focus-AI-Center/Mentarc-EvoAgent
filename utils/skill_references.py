"""Reference paths and discoverable links for multi-file skills."""

import re
from urllib.parse import quote


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
