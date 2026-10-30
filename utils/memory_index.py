"""Parse legacy typed entries and workspace-file links in MEMORY.md."""

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

MEMORY_TYPES = ("user", "feedback", "project", "reference")
_INDEX_LINE_RE = re.compile(
    r"^-\s*\[(?P<type>user|feedback|project|reference)\]\s*"
    r"(?:\[(?P<link_title>[^\]]+)\]\((?P<file_path>[^)]+)\)|(?P<title>.+?))"
    r"\s*\((?P<date>\d{4}-\d{2}-\d{2})\)\s*(?:—\s*(?P<summary>.*))?$"
)


@dataclass(frozen=True)
class MemoryIndexEntry:
    type: str
    title: str
    date: str
    summary: str
    file_path: str | None = None

    def index_line(self) -> str:
        title = self.title
        if self.file_path is not None:
            title = f"[{title}]({quote(self.file_path, safe='/-._~')})"
        return f"- [{self.type}] {title} ({self.date}) — {self.summary}"


def parse_memory_index(body: str) -> list[MemoryIndexEntry]:
    entries = []
    for line in body.splitlines():
        match = _INDEX_LINE_RE.match(line.strip())
        if match:
            path = match.group("file_path")
            entries.append(
                MemoryIndexEntry(
                    type=match.group("type"),
                    title=(match.group("link_title") or match.group("title")).strip(),
                    date=match.group("date"),
                    summary=(match.group("summary") or "").strip(),
                    file_path=unquote(path) if path is not None else None,
                )
            )
    return entries
