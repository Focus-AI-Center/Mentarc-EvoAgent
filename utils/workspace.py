"""Per-user workspace backed by a pluggable ``StorageBackend``.

Each user gets an isolated key-prefix: ``{user_id}/...`` containing
prompts, skills, sessions, images, and previews. Two backends are wired up
(see ``utils/storage_backend/__init__.py``):

  - ``LocalFSBackend``  → keys map 1:1 to files under ``WORKSPACE_ROOT``
  - ``MinIOSDKBackend`` → keys map 1:1 to objects in the MinIO bucket

The public ``Workspace`` API is intentionally synchronous to keep the many
existing call sites unchanged. Each method delegates to the backend's
``*_sync`` methods (one network round-trip per op for MinIO, ~µs for LocalFS).
Async fan-out (``asyncio.gather`` over the prompt-loading chain) is layered
on top via ``read_text_async`` for the few hot paths that benefit.

On first access the workspace is initialized by copying the platform-owned
templates from ``DEFAULT_PROMPTS_DIR`` and ``DEFAULT_SKILLS_DIR`` (still on
the server's local filesystem — they're packaged with the deployment) into
the user's prefix. ``SOUL.md`` is force-overwritten on every sync; user
files (``MEMORY.md`` etc.) are only seeded on first access.
"""

import base64
import hashlib
import json
import re
from pathlib import Path

from configs.settings import (
    DEFAULT_PROMPTS_DIR,
    DEFAULT_SKILLS_DIR,
)
from utils.storage_backend import KeyNotFoundError, StorageBackend, get_backend

_DEFAULTS_DIR = Path(DEFAULT_PROMPTS_DIR)
_DEFAULTS_SKILLS_DIR = Path(DEFAULT_SKILLS_DIR)


def _compute_sync_version() -> str:
    """Hash of platform-owned templates. Used to gate per-user re-sync."""
    parts: list[str] = []
    soul = _DEFAULTS_DIR / "SOUL.md"
    if soul.exists():
        st = soul.stat()
        parts.append(f"SOUL:{st.st_mtime_ns}:{st.st_size}")
    if _DEFAULTS_SKILLS_DIR.exists():
        for f in sorted(_DEFAULTS_SKILLS_DIR.rglob("*")):
            if f.is_file():
                st = f.stat()
                parts.append(
                    f"{f.relative_to(_DEFAULTS_SKILLS_DIR)}:{st.st_mtime_ns}:{st.st_size}"
                )
    return hashlib.md5("\n".join(parts).encode()).hexdigest() if parts else ""


_SYNC_VERSION: str = _compute_sync_version()


def _scan_skill_dirs(
    base_dir: Path, prefix: str = ""
) -> list[tuple[str, Path, list[tuple[str, Path]]]]:
    """Recursively scan a template tree for skills + their reference files."""
    results: list[tuple[str, Path, list[tuple[str, Path]]]] = []
    if not base_dir.exists():
        return results
    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        dir_name = entry.name
        yaml_name = None
        try:
            text = skill_file.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end != -1:
                    m = re.search(r"^name:\s*(.+)$", text[:end], re.MULTILINE)
                    if m:
                        yaml_name = m.group(1).strip()
        except (OSError, UnicodeDecodeError):
            pass
        base_name = yaml_name or dir_name
        skill_name = f"{prefix}/{base_name}" if prefix else base_name
        sub_skill_dirs = {
            child.name
            for child in entry.iterdir()
            if child.is_dir() and (child / "SKILL.md").exists()
        }
        ref_files: list[tuple[str, Path]] = []
        for f in sorted(entry.rglob("*")):
            if not f.is_file() or f.name == "SKILL.md":
                continue
            rel = f.relative_to(entry)
            if rel.parts[0] in sub_skill_dirs:
                continue
            ref_files.append((str(rel).replace("\\", "/"), f))
        results.append((skill_name, skill_file, ref_files))
        results.extend(_scan_skill_dirs(entry, skill_name))
    return results


def _validate_segment(segment: str, *, allow_slash: bool = False) -> None:
    """Reject path-traversal in user-supplied name segments.

    ``allow_slash=True`` permits nested skill names like ``parent/child`` while
    still rejecting ``..``, leading slashes, and empty parts.
    """
    if not segment:
        raise ValueError("Empty path segment")
    if segment.startswith("/") or segment.startswith("\\"):
        raise ValueError(f"Absolute segment not allowed: {segment!r}")
    parts = segment.replace("\\", "/").split("/")
    if not allow_slash and len(parts) > 1:
        raise ValueError(f"Slash not allowed in this segment: {segment!r}")
    for p in parts:
        if p in ("", ".", ".."):
            raise ValueError(f"Invalid segment {p!r} in {segment!r}")


class Workspace:
    """Per-user workspace, keys prefixed with ``{user_id}/``."""

    def __init__(self, user_id: str, backend: StorageBackend | None = None):
        if not user_id or "/" in user_id or user_id in (".", ".."):
            raise ValueError(f"Invalid user_id: {user_id!r}")
        self.user_id = user_id
        self._backend = backend if backend is not None else get_backend()
        self._init_workspace()

    # ── key helpers ──────────────────────────────────────────────────────

    def _key(self, *parts: str) -> str:
        """Build ``{user_id}/<parts>``; raise on traversal attempts in parts.

        The first part is one of ``prompts/skills/sessions/images/previews/.meta``
        and is always supplied by us — only the remaining parts come from the
        caller, so we validate those.
        """
        if not parts:
            raise ValueError("_key requires at least one part")
        # First part is always trusted (one of our subdirs).
        out = [self.user_id, parts[0]]
        for p in parts[1:]:
            _validate_segment(
                p, allow_slash=parts[0] in ("skills", "previews", "sessions", "images")
            )
            out.append(p)
        return "/".join(out)

    def _user_prefix(self) -> str:
        return f"{self.user_id}/"

    # ── primitive read / write through backend ───────────────────────────

    def _read_text(self, key: str) -> str:
        try:
            return self._backend.read_text_sync(key)
        except KeyNotFoundError:
            return ""

    def _write_text(self, key: str, content: str) -> None:
        self._backend.write_text_sync(key, content)

    def _read_bytes(self, key: str) -> bytes | None:
        try:
            return self._backend.read_bytes_sync(key)
        except KeyNotFoundError:
            return None

    def _write_bytes(self, key: str, data: bytes) -> None:
        self._backend.write_bytes_sync(key, data)

    def _read_json(self, key: str, default=None):
        try:
            raw = self._backend.read_text_sync(key)
        except KeyNotFoundError:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return default

    def _write_json(self, key: str, data) -> None:
        self._backend.write_text_sync(
            key, json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _exists(self, key: str) -> bool:
        return self._backend.head_etag_sync(key) is not None

    def _delete(self, key_or_prefix: str) -> int:
        return self._backend.delete_sync(key_or_prefix)

    def _list(self, prefix: str) -> list[str]:
        return self._backend.list_keys_sync(prefix)

    # ── Init ─────────────────────────────────────────────────────────────

    def _init_workspace(self) -> None:
        """Seed default prompts + builtin skills if not already at current version."""
        version_key = self._key(".meta", "sync_version")
        stored = self._read_text(version_key).strip()
        if stored == _SYNC_VERSION:
            return

        if _DEFAULTS_DIR.exists():
            for md in _DEFAULTS_DIR.glob("*.md"):
                target = self._key("prompts", md.name)
                # SOUL.md is platform-owned: always overwrite.
                # Others: only seed if missing (preserve user edits).
                if md.name == "SOUL.md" or not self._exists(target):
                    self._write_text(
                        target, md.read_text(encoding="utf-8")
                    )

        self.sync_builtin_skills()
        self._write_text(version_key, _SYNC_VERSION)

    # ── Prompts ──────────────────────────────────────────────────────────

    def read_prompt(self, name: str) -> str:
        _validate_segment(name)
        return self._read_text(self._key("prompts", f"{name}.md"))

    def write_prompt(self, name: str, content: str) -> None:
        _validate_segment(name)
        self._write_text(self._key("prompts", f"{name}.md"), content)

    def list_prompts(self) -> list[str]:
        names = set()
        if _DEFAULTS_DIR.exists():
            names.update(md.stem for md in _DEFAULTS_DIR.glob("*.md"))
        for k in self._list(f"{self.user_id}/prompts/"):
            tail = k[len(f"{self.user_id}/prompts/") :]
            if tail.endswith(".md") and "/" not in tail:
                names.add(tail[: -len(".md")])
        return sorted(names)

    # ── Skills ───────────────────────────────────────────────────────────

    def skill_set(self, name: str, value: str) -> None:
        self._write_text(self._key("skills", name, "SKILL.md"), value)

    def skill_get(self, name: str) -> str | None:
        key = self._key("skills", name, "SKILL.md")
        if not self._exists(key):
            return None
        try:
            return self._backend.read_text_sync(key)
        except KeyNotFoundError:
            return None

    def skill_get_all(self) -> dict[str, str]:
        result: dict[str, str] = {}
        prefix = f"{self.user_id}/skills/"
        for k in self._list(prefix):
            tail = k[len(prefix) :]
            if not tail.endswith("/SKILL.md"):
                continue
            name = tail[: -len("/SKILL.md")]
            try:
                result[name] = self._backend.read_text_sync(k)
            except KeyNotFoundError:
                continue
        return result

    def skill_del(self, name: str) -> bool:
        prefix = self._key("skills", name) + "/"
        # Use list+delete-prefix semantics; backend.delete handles recursion.
        if not self._list(prefix):
            return False
        self._delete(prefix)
        return True

    def skill_refs_get(self, name: str) -> dict[str, str]:
        refs: dict[str, str] = {}
        prefix = self._key("skills", name, "references") + "/"
        for k in self._list(prefix):
            rel = k[len(prefix) :]
            if not rel:
                continue
            try:
                refs[rel] = self._backend.read_text_sync(k)
            except KeyNotFoundError:
                pass
        return refs

    def skill_ref_set(self, name: str, filename: str, content: str) -> None:
        # filename can be nested under references/, e.g. "deep/file.md".
        _validate_segment(filename, allow_slash=True)
        key = self._key("skills", name, "references") + "/" + filename
        self._write_text(key, content)

    def skill_ref_del(self, name: str, filename: str) -> bool:
        _validate_segment(filename, allow_slash=True)
        key = self._key("skills", name, "references") + "/" + filename
        if not self._exists(key):
            return False
        self._delete(key)
        return True

    # ── Sessions ─────────────────────────────────────────────────────────

    def history_load(self, session_id: str) -> dict | None:
        return self._read_json(self._key("sessions", f"{session_id}.json"))

    def history_save(self, session_id: str, data: dict) -> None:
        self._write_json(self._key("sessions", f"{session_id}.json"), data)

    def history_del(self, session_id: str) -> bool:
        key = self._key("sessions", f"{session_id}.json")
        if not self._exists(key):
            return False
        self._delete(key)
        return True

    def list_sessions(self) -> list[dict]:
        result = []
        prefix = f"{self.user_id}/sessions/"
        for k in self._list(prefix):
            tail = k[len(prefix) :]
            if not tail.endswith(".json") or "/" in tail:
                continue
            data = self._read_json(k)
            if not isinstance(data, dict):
                continue
            preview = ""
            for msg in data.get("conversation", []):
                if msg.get("role") == "user":
                    preview = str(msg.get("content", ""))[:80]
                    break
            result.append(
                {
                    "session_id": tail[: -len(".json")],
                    "timestamp": data.get("timestamp", ""),
                    "message_count": data.get("message_count", 0),
                    "preview": preview,
                }
            )
        return sorted(result, key=lambda s: s.get("timestamp", ""), reverse=True)

    def clear_sessions(self) -> int:
        prefix = f"{self.user_id}/sessions/"
        keys = self._list(prefix)
        if keys:
            self._delete(prefix)
        return len(keys)

    # ── Skills bulk ──────────────────────────────────────────────────────

    def clear_skills(self) -> int:
        prefix = f"{self.user_id}/skills/"
        # Count distinct top-level skill dirs (legacy semantics: number of
        # skill directories removed, not number of files).
        skill_dirs: set[str] = set()
        for k in self._list(prefix):
            tail = k[len(prefix) :]
            if "/" in tail:
                skill_dirs.add(tail.split("/", 1)[0])
        if skill_dirs:
            self._delete(prefix)
        return len(skill_dirs)

    def clear_all(self) -> dict:
        return {"sessions": self.clear_sessions(), "skills": self.clear_skills()}

    def delete_workspace(self) -> dict:
        cleared = {
            "sessions": self.clear_sessions(),
            "skills": self.clear_skills(),
            "images": self.clear_images(),
            "previews": self.clear_previews(),
            "prompts": sum(
                1
                for k in self._list(f"{self.user_id}/prompts/")
                if k.endswith(".md")
            ),
        }
        self._delete(self._user_prefix())
        return cleared

    def reset_to_defaults(self) -> dict:
        cleared = self.delete_workspace()
        self._init_workspace()
        return cleared

    def sync_builtin_skills(self) -> dict:
        added, updated = 0, 0
        existing = set(self.skill_get_all())
        for skill_name, skill_file, ref_files in _scan_skill_dirs(_DEFAULTS_SKILLS_DIR):
            self.skill_set(skill_name, skill_file.read_text(encoding="utf-8"))
            # Wipe and re-sync references.
            refs_prefix = self._key("skills", skill_name, "references") + "/"
            self._delete(refs_prefix)
            for rel, file_path in ref_files:
                norm = (
                    rel[len("references/") :]
                    if rel.startswith("references/")
                    else rel
                )
                try:
                    self.skill_ref_set(
                        skill_name, norm, file_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError):
                    pass
            if skill_name in existing:
                updated += 1
            else:
                added += 1
        return {"added": added, "updated": updated}

    # ── MCP config ───────────────────────────────────────────────────────

    def mcp_config_load(self) -> dict:
        """Load optional user MCP registry from ``.meta/mcp.json``.

        Missing or malformed files return an empty registry. The file is not
        seeded by default and is intentionally separate from prompt/skill
        storage so MCP remains an opt-in user configuration.
        """
        data = self._read_json(self._key(".meta", "mcp.json"), default={})
        return data if isinstance(data, dict) else {}

    # ── Images ───────────────────────────────────────────────────────────

    def image_write(self, filename: str, data: bytes) -> bool:
        self._write_bytes(self._key("images", filename), data)
        return True

    def image_read(self, filename: str) -> bytes | None:
        return self._read_bytes(self._key("images", filename))

    def image_list(self) -> list[str]:
        prefix = f"{self.user_id}/images/"
        out: list[str] = []
        for k in self._list(prefix):
            tail = k[len(prefix) :]
            if "/" not in tail:
                out.append(tail)
        return sorted(out)

    def image_del(self, filename: str) -> bool:
        key = self._key("images", filename)
        if not self._exists(key):
            return False
        self._delete(key)
        return True

    def clear_images(self) -> int:
        names = self.image_list()
        if names:
            for n in names:
                self._delete(self._key("images", n))
        return len(names)

    # ── Previews ─────────────────────────────────────────────────────────

    def preview_write(self, preview_id: str, html: str) -> bool:
        self._write_text(self._key("previews", f"{preview_id}.html"), html)
        return True

    def preview_read(self, preview_id: str) -> str | None:
        key = self._key("previews", f"{preview_id}.html")
        if not self._exists(key):
            return None
        try:
            return self._backend.read_text_sync(key)
        except KeyNotFoundError:
            return None

    def preview_list(self) -> list[str]:
        prefix = f"{self.user_id}/previews/"
        out: list[str] = []
        for k in self._list(prefix):
            tail = k[len(prefix) :]
            if tail.endswith(".html") and "/" not in tail:
                out.append(tail[: -len(".html")])
        return sorted(out)

    def preview_del(self, preview_id: str) -> bool:
        key = self._key("previews", f"{preview_id}.html")
        if not self._exists(key):
            return False
        self._delete(key)
        return True

    def clear_previews(self) -> int:
        ids = self.preview_list()
        if ids:
            for pid in ids:
                self._delete(self._key("previews", f"{pid}.html"))
        return len(ids)

    # ── Snapshot ─────────────────────────────────────────────────────────

    def export_snapshot(self) -> dict:
        prompts = {
            name: self.read_prompt(name)
            for name in self.list_prompts()
            if self.read_prompt(name)
        }
        skills = {}
        for name, content in self.skill_get_all().items():
            skills[name] = {"content": content, "refs": self.skill_refs_get(name)}
        sessions = {}
        for item in self.list_sessions():
            sid = item["session_id"]
            record = self.history_load(sid)
            if record is not None:
                sessions[sid] = record
        images = {}
        for filename in self.image_list():
            data = self.image_read(filename)
            if data:
                images[filename] = base64.b64encode(data).decode("ascii")
        previews = {}
        for pid in self.preview_list():
            html = self.preview_read(pid)
            if html:
                previews[pid] = html
        return {
            "version": 1,
            "user_id": self.user_id,
            "prompts": prompts,
            "skills": skills,
            "sessions": {"index": self.list_sessions(), "records": sessions},
            "images": images,
            "previews": previews,
        }

    def import_snapshot(self, snapshot: dict, replace: bool = True) -> None:
        if replace:
            self.reset_to_defaults()
        for name, content in (snapshot.get("prompts") or {}).items():
            if isinstance(name, str) and isinstance(content, str):
                self.write_prompt(name, content)
        for skill_name, payload in (snapshot.get("skills") or {}).items():
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str):
                    self.skill_set(skill_name, content)
                for ref_name, ref_content in (payload.get("refs") or {}).items():
                    if isinstance(ref_name, str) and isinstance(ref_content, str):
                        self.skill_ref_set(skill_name, ref_name, ref_content)
        for sid, record in (
            snapshot.get("sessions", {}).get("records") or {}
        ).items():
            if isinstance(sid, str) and isinstance(record, dict):
                self.history_save(sid, record)
        for filename, b64_data in (snapshot.get("images") or {}).items():
            if isinstance(filename, str) and isinstance(b64_data, str):
                try:
                    self.image_write(
                        filename, base64.b64decode(b64_data.encode("ascii"))
                    )
                except ValueError:
                    pass
        for preview_id, html in (snapshot.get("previews") or {}).items():
            if isinstance(preview_id, str) and isinstance(html, str):
                self.preview_write(preview_id, html)
