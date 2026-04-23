"""Per-user workspace backed by local filesystem.

Each user gets an isolated directory: {WORKSPACE_ROOT}/{user_id}/
containing prompts, skills, sessions, images, and previews.

On first access the workspace is initialized by copying default prompts
from configs/prompts/ and default skills from configs/skills/.
"""

import base64
import hashlib
import json
import re
import shutil
from pathlib import Path

from configs.settings import (
    DEFAULT_PROMPTS_DIR,
    DEFAULT_SKILLS_DIR,
    WORKSPACE_ROOT,
)

_DEFAULTS_DIR = Path(DEFAULT_PROMPTS_DIR)
_DEFAULTS_SKILLS_DIR = Path(DEFAULT_SKILLS_DIR)


def _compute_sync_version() -> str:
    """Compute a version hash from local configs (prompts + skills)."""
    parts: list[str] = []
    soul = _DEFAULTS_DIR / "SOUL.md"
    if soul.exists():
        st = soul.stat()
        parts.append(f"SOUL:{st.st_mtime_ns}:{st.st_size}")
    if _DEFAULTS_SKILLS_DIR.exists():
        for f in sorted(_DEFAULTS_SKILLS_DIR.rglob("*")):
            if f.is_file():
                st = f.stat()
                parts.append(f"{f.relative_to(_DEFAULTS_SKILLS_DIR)}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.md5("\n".join(parts).encode()).hexdigest() if parts else ""


_SYNC_VERSION: str = _compute_sync_version()


def _scan_skill_dirs(base_dir: Path, prefix: str = "") -> list[tuple[str, Path, list[tuple[str, Path]]]]:
    """Recursively scan for skills in a directory tree."""
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
            child.name for child in entry.iterdir() if child.is_dir() and (child / "SKILL.md").exists()
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


class Workspace:
    """Local filesystem per-user workspace."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.root = Path(WORKSPACE_ROOT).resolve() / user_id
        self.prompts_dir = self.root / "prompts"
        self.skills_dir = self.root / "skills"
        self.sessions_dir = self.root / "sessions"
        self.images_dir = self.root / "images"
        self.previews_dir = self.root / "previews"
        self.meta_dir = self.root / ".meta"
        self._ensure_dirs()
        self._init_workspace()

    def _ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.prompts_dir,
            self.skills_dir,
            self.sessions_dir,
            self.images_dir,
            self.previews_dir,
            self.meta_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _safe_join(self, *parts: str) -> Path:
        """Join ``parts`` under ``self.root`` and verify the result stays confined
        to the subtree indicated by ``parts[0]`` (prompts/skills/sessions/…).

        Guards every public method that accepts user-controlled names against
        path traversal via ``..`` or absolute segments. The check is stronger
        than "under user root" — it also rejects cross-bucket escapes (e.g.
        a skill-ref filename that climbs out of ``skills/`` into ``.meta/``).

        ``Path.resolve()`` runs without ``strict=True`` so the target need not
        exist yet (matters for writes). No public method here creates symlinks,
        so a malicious one would require shell-level access to plant.

        Raises ``ValueError`` on escape; propagates to the caller.
        """
        if not parts:
            raise ValueError("_safe_join requires at least one part")
        base = (self.root / parts[0]).resolve()
        resolved = self.root.joinpath(*parts).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as err:
            raise ValueError(f"Path escapes intended subtree {parts[0]!r}: {parts!r}") from err
        return resolved

    # ── File helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _text_read(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    @staticmethod
    def _text_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _json_read(path: Path, default=None):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return default

    @staticmethod
    def _json_write(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _bytes_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    # ── Init ─────────────────────────────────────────────────────────────

    def _init_workspace(self) -> None:
        """Copy default prompts and builtin skills on first access."""
        version_file = self.meta_dir / "sync_version"
        stored = self._text_read(version_file).strip()
        if stored == _SYNC_VERSION:
            return  # Already synced

        # Copy default prompts (only if not exist)
        if _DEFAULTS_DIR.exists():
            for md in _DEFAULTS_DIR.glob("*.md"):
                target = self.prompts_dir / md.name
                if not target.exists():
                    target.write_text(md.read_text(encoding="utf-8"), encoding="utf-8")

            # Always overwrite SOUL.md with latest
            soul = _DEFAULTS_DIR / "SOUL.md"
            if soul.exists():
                self._text_write(self.prompts_dir / "SOUL.md", soul.read_text(encoding="utf-8"))

        # Sync builtin skills
        self.sync_builtin_skills()

        # Mark as synced
        self._text_write(version_file, _SYNC_VERSION)

    # ── Prompts ──────────────────────────────────────────────────────────

    def read_prompt(self, name: str) -> str:
        return self._text_read(self._safe_join("prompts", f"{name}.md"))

    def write_prompt(self, name: str, content: str) -> None:
        self._text_write(self._safe_join("prompts", f"{name}.md"), content)

    def list_prompts(self) -> list[str]:
        names = set()
        if _DEFAULTS_DIR.exists():
            names.update(md.stem for md in _DEFAULTS_DIR.glob("*.md"))
        names.update(md.stem for md in self.prompts_dir.glob("*.md"))
        return sorted(names)

    # ── Skills ───────────────────────────────────────────────────────────

    def skill_set(self, name: str, value: str) -> None:
        self._text_write(self._safe_join("skills", name, "SKILL.md"), value)

    def skill_get(self, name: str) -> str | None:
        path = self._safe_join("skills", name, "SKILL.md")
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def skill_get_all(self) -> dict[str, str]:
        result = {}
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            rel = skill_file.parent.relative_to(self.skills_dir).as_posix()
            content = self.skill_get(rel)
            if content is not None:
                result[rel] = content
        return result

    def skill_del(self, name: str) -> bool:
        skill_dir = self._safe_join("skills", name)
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir, ignore_errors=True)
        return True

    def skill_refs_get(self, name: str) -> dict[str, str]:
        refs: dict[str, str] = {}
        refs_dir = self._safe_join("skills", name, "references")
        if not refs_dir.exists():
            return refs
        for ref_file in refs_dir.rglob("*"):
            if ref_file.is_file():
                try:
                    refs[ref_file.relative_to(refs_dir).as_posix()] = ref_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    pass
        return refs

    def skill_ref_set(self, name: str, filename: str, content: str) -> None:
        self._text_write(self._safe_join("skills", name, "references", filename), content)

    def skill_ref_del(self, name: str, filename: str) -> bool:
        path = self._safe_join("skills", name, "references", filename)
        if not path.exists():
            return False
        path.unlink()
        return True

    # ── Sessions ─────────────────────────────────────────────────────────

    def history_load(self, session_id: str) -> dict | None:
        return self._json_read(self._safe_join("sessions", f"{session_id}.json"))

    def history_save(self, session_id: str, data: dict) -> None:
        self._json_write(self._safe_join("sessions", f"{session_id}.json"), data)

    def history_del(self, session_id: str) -> bool:
        path = self._safe_join("sessions", f"{session_id}.json")
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_sessions(self) -> list[dict]:
        result = []
        for path in self.sessions_dir.glob("*.json"):
            data = self._json_read(path)
            if not isinstance(data, dict):
                continue
            preview = ""
            for msg in data.get("conversation", []):
                if msg.get("role") == "user":
                    preview = str(msg.get("content", ""))[:80]
                    break
            result.append(
                {
                    "session_id": path.stem,
                    "timestamp": data.get("timestamp", ""),
                    "message_count": data.get("message_count", 0),
                    "preview": preview,
                }
            )
        return sorted(result, key=lambda s: s.get("timestamp", ""), reverse=True)

    def clear_sessions(self) -> int:
        sessions = list(self.sessions_dir.glob("*.json"))
        for p in sessions:
            p.unlink(missing_ok=True)
        return len(sessions)

    # ── Skills bulk ──────────────────────────────────────────────────────

    def clear_skills(self) -> int:
        dirs = [p for p in self.skills_dir.iterdir() if p.is_dir()]
        for p in dirs:
            shutil.rmtree(p, ignore_errors=True)
        return len(dirs)

    def clear_all(self) -> dict:
        return {"sessions": self.clear_sessions(), "skills": self.clear_skills()}

    def delete_workspace(self) -> dict:
        cleared = {
            "sessions": self.clear_sessions(),
            "skills": self.clear_skills(),
            "images": self.clear_images(),
            "previews": self.clear_previews(),
            "prompts": len(list(self.prompts_dir.glob("*.md"))),
        }
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        return cleared

    def reset_to_defaults(self) -> dict:
        cleared = self.delete_workspace()
        self._ensure_dirs()
        self._init_workspace()
        return cleared

    def sync_builtin_skills(self) -> dict:
        added, updated = 0, 0
        existing = set(self.skill_get_all())
        for skill_name, skill_file, ref_files in _scan_skill_dirs(_DEFAULTS_SKILLS_DIR):
            self.skill_set(skill_name, skill_file.read_text(encoding="utf-8"))
            # Clear and re-sync references
            refs_dir = self.skills_dir / skill_name / "references"
            if refs_dir.exists():
                shutil.rmtree(refs_dir, ignore_errors=True)
            for rel, file_path in ref_files:
                # skill_ref_set already writes under "references/"; strip any
                # leading "references/" from the scanned relative path so we
                # don't end up at skills/{name}/references/references/...
                norm = rel[len("references/") :] if rel.startswith("references/") else rel
                try:
                    self.skill_ref_set(skill_name, norm, file_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    pass
            if skill_name in existing:
                updated += 1
            else:
                added += 1
        return {"added": added, "updated": updated}

    # ── Images ───────────────────────────────────────────────────────────

    def image_write(self, filename: str, data: bytes) -> bool:
        self._bytes_write(self._safe_join("images", filename), data)
        return True

    def image_read(self, filename: str) -> bytes | None:
        path = self._safe_join("images", filename)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def image_list(self) -> list[str]:
        return sorted(p.name for p in self.images_dir.iterdir() if p.is_file())

    def image_del(self, filename: str) -> bool:
        path = self._safe_join("images", filename)
        if not path.exists():
            return False
        path.unlink()
        return True

    def clear_images(self) -> int:
        files = [p for p in self.images_dir.iterdir() if p.is_file()]
        for p in files:
            p.unlink()
        return len(files)

    # ── Previews ─────────────────────────────────────────────────────────

    def preview_write(self, preview_id: str, html: str) -> bool:
        self._text_write(self._safe_join("previews", f"{preview_id}.html"), html)
        return True

    def preview_read(self, preview_id: str) -> str | None:
        path = self._safe_join("previews", f"{preview_id}.html")
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def preview_list(self) -> list[str]:
        return sorted(p.stem for p in self.previews_dir.glob("*.html"))

    def preview_del(self, preview_id: str) -> bool:
        path = self._safe_join("previews", f"{preview_id}.html")
        if not path.exists():
            return False
        path.unlink()
        return True

    def clear_previews(self) -> int:
        files = list(self.previews_dir.glob("*.html"))
        for p in files:
            p.unlink()
        return len(files)

    # ── Snapshot ─────────────────────────────────────────────────────────

    def export_snapshot(self) -> dict:
        prompts = {name: self.read_prompt(name) for name in self.list_prompts() if self.read_prompt(name)}
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
        for sid, record in (snapshot.get("sessions", {}).get("records") or {}).items():
            if isinstance(sid, str) and isinstance(record, dict):
                self.history_save(sid, record)
        for filename, b64_data in (snapshot.get("images") or {}).items():
            if isinstance(filename, str) and isinstance(b64_data, str):
                try:
                    self.image_write(filename, base64.b64decode(b64_data.encode("ascii")))
                except ValueError:
                    pass
        for preview_id, html in (snapshot.get("previews") or {}).items():
            if isinstance(preview_id, str) and isinstance(html, str):
                self.preview_write(preview_id, html)
