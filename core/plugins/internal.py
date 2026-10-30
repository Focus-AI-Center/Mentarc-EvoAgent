"""Internal tools - always loaded, not part of plugin registry.

These tools allow the Agent to self-manage user profiles, skills,
and memory during conversation.
"""

import json
import re
import shlex
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from core.agent_compat import BaseTool
from core.skills.manager import SkillManager
from utils.context_assets import memory_index_notice, skill_location_context
from utils.logger import logger
from utils.memory_index import MEMORY_TYPES, MemoryIndexEntry, parse_memory_index
from utils.skill_references import normalize_reference_path, normalize_references
from utils.tool_names import normalize_tool_references
from utils.workspace import Workspace
from utils.ws_call import ws_call

# --- User Profile Tool ---


class UpdateUserProfileParams(BaseModel):
    """Update user profile fields. Supports both full update (update) and incremental append (append)."""

    action: Literal["update", "append"] = Field(
        description="Action: update replaces supplied values; append merges supplied values (recommended)"
    )
    # Basic profile fields
    name: str | None = Field(default=None, description="User name")
    nickname: str | None = Field(default=None, description="Preferred name/nickname")
    timezone: str | None = Field(default=None, description="User timezone, such as Asia/Shanghai (GMT+8)")
    workspace: str | None = Field(default=None, description="User workspace path")
    social_accounts: str | None = Field(
        default=None, description="Social accounts,separate multiple values with newlines"
    )

    # Business profile fields
    main_products: str | None = Field(
        default=None, description="Main products, product names or business focus"
    )
    company_name: str | None = Field(default=None, description="Company name, full or abbreviated")
    company_website: str | None = Field(default=None, description="Company website URL")
    company_scale: str | None = Field(
        default=None,
        description="Company scale, such as individual business, small team or established company",
    )
    current_stage: str | None = Field(
        default=None,
        description="Current stage/goals(starting out/entering new markets/expanding sales channels/growing the customer base/improving operational efficiency)",
    )
    main_industry: str | None = Field(default=None, description="Main industry(Company industry)")
    target_markets: str | None = Field(
        default=None, description="Target markets(global regions,continents/countries/scope)"
    )
    platform_preference: str | None = Field(
        default=None, description="Preferred B2B, B2C, social or company website platforms"
    )
    style_preference: str | None = Field(
        default=None, description="Style preferences(Language style,brand style,visual style)"
    )
    business_strategy: str | None = Field(
        default=None,
        description="Business strategy(sample policy,pricing,follow-up approach,payment preferences and other company practices)",
    )

    # General profile fields
    background: str | None = Field(default=None, description="industry,role,interests,work")
    preferences: str | None = Field(
        default=None, description="language,communication style,Output format preferences"
    )

    multi_field_append: bool | None = Field(
        default=True, description="Append to multiline fields instead of replacing them(append only)"
    )


# Tool parameter -> English USER.md field label
_PARAM_TO_FIELD = {
    # Basic information
    "name": "Name",
    "nickname": "Nickname",
    "timezone": "Timezone",
    "workspace": "Workspace",
    "social_accounts": "Social accounts",
    # Business information
    "main_products": "Main products",
    "company_name": "Company name",
    "company_website": "Company website",
    "company_scale": "Company scale",
    "current_stage": "Current stage/goals",
    "main_industry": "Main industry",
    "target_markets": "Target markets",
    "platform_preference": "Platform preferences",
    "style_preference": "Style preferences",
    "business_strategy": "Business strategy",
    # General information
    "background": "Background",
    "preferences": "Preferences",
}


class UpdateUserProfileTool(BaseTool):
    name: str = "update_user_profile"
    description: str = (
        "Update individual user profile fields. Supports two modes:\n"
        "- update: Replace supplied fields and preserve omitted values\n"
        "- append: Merge supplied values, preserve omitted fields and append multiline content"
    )
    param_anno = UpdateUserProfileParams

    def __init__(self, user_id: str, workspace: "Workspace" = None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._ws = workspace or Workspace(user_id)

    async def __call__(self, params: UpdateUserProfileParams) -> str:
        try:
            # Determine operation type
            is_append = params.action == "append"

            # Map tool parameters to profile labels, skipping omitted values
            updates = {}
            for parameter, label in _PARAM_TO_FIELD.items():
                val = getattr(params, parameter)
                if val is not None:
                    updates[label] = val

            if is_append:
                updated = await _append_user_profile_fields(
                    self._user_id, updates, params.multi_field_append, ws=self._ws
                )
            else:
                updated = await _update_user_profile_fields(self._user_id, updates, ws=self._ws)

            if not updated:
                return json.dumps(
                    {"success": False, "message": "No valid fields were updated."}, ensure_ascii=False
                )

            # Read updated fields' full values from profile after write
            ws = self._ws
            raw = await ws_call(ws, "read_prompt", "USER") or ""
            body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
            all_fields = _parse_user_fields(body)
            # Only the fields that were updated, with their complete post-update values
            fields = {k: all_fields.get(k, "") for k in updated}

            action_text = "Appended" if is_append else "Updated"
            fields_str = ", ".join(updated)
            logger.info(f"{action_text} user profile fields [{fields_str}] for {self._user_id}")
            return json.dumps(
                {
                    "success": True,
                    "action": action_text,
                    "fields": fields,
                    "message": f"{action_text} fields: {fields_str}",
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"Failed to update user profile: {e}")
            return json.dumps(
                {"success": False, "message": f"Operation failed: {str(e)}"}, ensure_ascii=False
            )


# --- Skill Manager Tool (read-only) ---


class SkillParams(BaseModel):
    action: Literal["list", "get", "get_ref", "save", "delete", "ref_set", "ref_del"] = Field(
        description=(
            "Actions: list, get (main document), get_ref (reference body), "
            "save (effective immediately), delete, "
            "ref_set (write reference), ref_del (delete reference)"
        )
    )
    name: str = Field(default="", description="Skill name; optional for list and required for other actions")
    ref_filename: str = Field(
        default="",
        description="Reference path relative to references/ path (required for get_ref/ref_set/ref_del, such as rules.md)",
    )
    # --- save only fields ---
    description: str = Field(default="", description="Skill description(save required)")
    triggers: list[str] = Field(default=[], description="Trigger phrases(save required)")
    instructions: str = Field(default="", description="Skill execution steps(save required)")
    references: dict[str, str] = Field(
        default_factory=dict,
        description='Optional for save: mapping of reference paths to complete text, such as {"rules.md":"Detailed rules"},saved together with the main document',
    )

    @field_validator("references")
    @classmethod
    def validate_references(cls, value):
        return normalize_references(value)

    required_tools: list[str] = Field(default=[], description="Required tool names(save optional)")
    tags: list[str] = Field(default=[], description="Category tags(save optional)")
    source: str = Field(
        default="user_defined", description="Optional source for save: user_defined or extracted"
    )
    # --- ref_set only fields ---
    ref_content: str = Field(default="", description="Reference file content(ref_set required)")


class SkillTool(BaseTool):
    name: str = "skill"
    description: str = (
        "Manage skills.\n"
        "- list: List skills\n"
        "- get: Load the main document and reference list\n"
        "- get_ref: Read a reference body\n"
        "- save: Save/Create skill(effective immediately)\n"
        "- delete: Delete skill\n"
        "- ref_set: Write a reference file\n"
        "- ref_del: Delete a reference file"
    )
    param_anno = SkillParams

    def __init__(
        self,
        user_id: str,
        skill_manager: SkillManager,
        workspace: "Workspace" = None,
        session_id: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._session_id = session_id
        self._skill_manager = skill_manager
        self._ws = workspace or Workspace(user_id)
        self._got_skills: set = set()

    async def __call__(self, params: SkillParams) -> str:
        try:
            if params.action == "list":
                return self._action_list(params.name)
            elif params.action == "get":
                return await self._action_get(params.name)
            elif params.action == "get_ref":
                return await self._action_get_ref(params.name, params.ref_filename)
            elif params.action == "save":
                return self._action_save(params)
            elif params.action == "delete":
                return self._action_delete(params.name)
            elif params.action == "ref_set":
                return await self._action_ref_set(params.name, params.ref_filename, params.ref_content)
            elif params.action == "ref_del":
                return await self._action_ref_del(params.name, params.ref_filename)
            else:
                return f"Unknown action: {params.action}"
        except Exception as e:
            logger.error(f"skill {params.action} failed: {e}")
            return f"Operation failed: {str(e)}"

    def _action_list(self, parent: str = "") -> str:
        all_skills = self._skill_manager.get_all_skills(self._user_id)
        if not all_skills:
            return "No confirmed skills available."

        if parent:
            prefix = parent.rstrip("/") + "/"
            children = {
                k: v for k, v in all_skills.items() if k.startswith(prefix) and "/" not in k[len(prefix) :]
            }
            if not children:
                return f"Skill '{parent}' has no child skills."
            lines = [f"- **{s.name}**: {s.description}" for s in children.values()]
            return f"'{parent}' child skills ({len(children)}):\n" + "\n".join(lines)

        top_level = {k: v for k, v in all_skills.items() if "/" not in k}
        if not top_level:
            return "No confirmed skills available."
        lines = []
        for s in top_level.values():
            triggers = ", ".join(s.triggers) if s.triggers else "none"
            lines.append(
                f"- **{s.name}** (v{s.version}, used {s.usage_count} times): {s.description} [Triggers: {triggers}]"
            )
        return f"Confirmed skills ({len(top_level)}):\n" + "\n".join(lines)

    async def _action_get(self, name: str) -> str:
        if not name:
            return "Provide a skill name."
        if name in self._got_skills:
            return f"Skill '{name}' loaded,no need to load it again."

        raw = await ws_call(self._ws, "skill_get", name)
        if not raw:
            return f"Skill '{name}' does not exist."
        self._got_skills.add(name)
        raw = normalize_tool_references(raw)

        result = f"Skill document `{name}/SKILL.md`:\n\n```markdown\n{raw}\n```"
        result += f"\n\n{skill_location_context(name)}"
        refs = await ws_call(self._ws, "skill_refs_get", name)
        if refs:
            result += f"\n\nAvailable references ({len(refs)}):"
            for filename, content in refs.items():
                result += f"\n- `{filename}` ({len(content)} characters) → skill(action='get_ref', name='{name}', ref_filename='{filename}')"
        return result

    async def _action_get_ref(self, name: str, ref_filename: str) -> str:
        if not name or not ref_filename:
            return "Provide a skill name and reference path."
        refs = await ws_call(self._ws, "skill_refs_get", name)
        normalized = normalize_reference_path(ref_filename)
        ref_filename = normalized if normalized in refs else ref_filename
        if ref_filename not in refs:
            return f"Reference file '{ref_filename}' is not present in skill '{name}'."
        return f"`skills/{name}/references/{ref_filename}`:\n\n```\n{refs[ref_filename]}\n```"

    def _action_save(self, params: SkillParams) -> str:
        if not params.name or not params.description or not params.instructions:
            return "save requires name, description and instructions."
        from core.skills import Skill

        skill = Skill(
            name=params.name,
            description=params.description,
            triggers=params.triggers,
            instructions=params.instructions,
            references=params.references,
            required_tools=params.required_tools,
            tags=params.tags,
            source=params.source if params.source in ("user_defined", "extracted") else "user_defined",
            extracted_from=self._session_id,
        )
        self._skill_manager.save_user_skill(self._user_id, skill)
        logger.info(f"Saved skill '{params.name}' for user {self._user_id}")
        return f"Skill '{params.name}' saved and effective immediately."

    def _action_delete(self, name: str) -> str:
        if not name:
            return "Provide a skill name."
        ok = self._skill_manager.delete_user_skill(self._user_id, name)
        return f"Skill '{name}' deleted." if ok else f"Skill '{name}' does not exist."

    async def _action_ref_set(self, name: str, ref_filename: str, ref_content: str) -> str:
        if not name or not ref_filename or not ref_content:
            return "ref_set requires name, ref_filename and ref_content."
        await ws_call(self._ws, "skill_ref_set", name, normalize_reference_path(ref_filename), ref_content)
        logger.info(f"Set ref '{ref_filename}' for skill '{name}'")
        return f"Wrote reference '{ref_filename}' in skill '{name}'."

    async def _action_ref_del(self, name: str, ref_filename: str) -> str:
        if not name or not ref_filename:
            return "ref_del requires name and ref_filename."
        ok = await ws_call(self._ws, "skill_ref_del", name, normalize_reference_path(ref_filename))
        return (
            f"Deleted reference '{ref_filename}'."
            if ok
            else f"Reference file '{ref_filename}' does not exist."
        )


# --- Memory Tool (index + workspace files or legacy per-type files) ---
_GET_BODY_MAX_CHARS = 8000  # Longer bodies can be read through sandbox bash.
_SUMMARY_MAX_CHARS = 40  # Soft cap on index-line summary length


class MemoryParams(BaseModel):
    action: Literal["read", "get", "append", "rewrite", "rebuild_index"] = Field(
        description=(
            "read: Return the index; get: Read an indexed file or grouped section by title; "
            "append: Register an existing file when file_path is provided, otherwise write grouped content; "
            "rewrite: Replace one grouped file; rebuild_index: Rescan grouped files and preserve file links."
        )
    )
    title: str | None = Field(
        default=None, description="Memory title; required for get/append and unique within its type"
    )
    type: Literal["user", "feedback", "project", "reference"] | None = Field(
        default=None,
        description=(
            "user: Additional user background; feedback: Collaboration rules and corrections; "
            "project: Project context; reference: External system pointers. "
            "Required for append/rewrite; inferred from the index for get when omitted."
        ),
    )
    content: str | None = Field(
        default=None, description="One entry for append, or the complete grouped file for rewrite"
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "Optional for append: an existing UTF-8 file created with execute_code, relative to the user workspace, "
            "such as memory/customer_acme.md. Only the index is updated; summary is required and content must be omitted. "
            "Absolute paths and parent traversal are rejected. get resolves this path from the title."
        ),
    )
    summary: str | None = Field(
        default=None,
        description=f"One-line index summary, up to {_SUMMARY_MAX_CHARS} characters. Required for file registration; grouped append defaults to the first body line.",
    )


class _MemoryConflict(ValueError):
    """A concurrent update or duplicate entry needs a user-visible response."""


class MemoryTool(BaseTool):
    name: str = "memory"
    description: str = (
        "Read and write long-term memory. MEMORY.md is the index already included in the system prompt.\n"
        "- read: Return the full current index.\n"
        f"- get(title, type?): Read up to {_GET_BODY_MAX_CHARS} body characters; use sandbox bash for longer files.\n"
        "- append(type, title, file_path, summary): Register a file created with execute_code; append only its index line.\n"
        "- append(type, title, content, summary?): Write a grouped entry and update the index.\n"
        "- rewrite(type, content): Replace an entire grouped file and rebuild its index entries.\n"
        "- rebuild_index: Rescan grouped entries while preserving independent file links."
    )
    param_anno = MemoryParams

    def __init__(self, user_id: str, workspace: "Workspace" = None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._ws = workspace or Workspace(user_id)

    async def __call__(self, params: MemoryParams) -> str:
        try:
            action = params.action
            if action == "read":
                return await self._action_read()
            if action == "get":
                if not params.title:
                    return "get action requires title."
                return await self._action_get(params.title, params.type)
            if action == "append":
                if not params.type or not params.title:
                    return "append requires type + title,and file_path + summary or content."
                if any(c in params.title for c in "\r\n[]"):
                    return "title cannot contain newlines or square brackets."
                if params.file_path is not None:
                    if params.content is not None:
                        return "Do not pass content when registering a file; create or edit it with execute_code first."
                    return await self._action_register_file(
                        params.type, params.title, params.file_path, params.summary
                    )
                if not params.content:
                    return "append requires content,or an existing file's file_path + summary."
                return await self._action_append(params.type, params.title, params.content, params.summary)
            if action == "rewrite":
                if not params.type or not params.content:
                    return "rewrite requires type + content."
                return await self._action_rewrite(params.type, params.content)
            if action == "rebuild_index":
                return await self._action_rebuild_index()
            return f"Unknown action: {action}"
        except _MemoryConflict as e:
            return str(e)
        except Exception as e:
            logger.error(f"memory {params.action} failed: {e}")
            return f"Operation failed: {str(e)}"

    # ── Shared I/O helpers ─────────────────────────────────

    async def _read_file(self, name: str) -> tuple[str, str]:
        """Return (frontmatter, body) from a prompt file. Missing file -> ('', '')."""
        raw = await ws_call(self._ws, "read_prompt", name) or ""
        if not raw.strip():
            return "", ""
        m = _FRONTMATTER_RE.match(raw)
        frontmatter = m.group(1) if m else ""
        body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
        return frontmatter, body

    async def _update_file(
        self, name: str, transform: Callable[[str], str], default_description: str = ""
    ) -> str:
        """Transform the latest body atomically, preserving its frontmatter."""

        def update(raw: str) -> str:
            m = _FRONTMATTER_RE.match(raw)
            frontmatter = (
                m.group(1) if m else (f"---\nname: {name}\ndescription: {default_description}\n---\n")
            )
            body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
            return frontmatter + "\n" + transform(body).rstrip() + "\n"

        raw = await ws_call(self._ws, "update_prompt", name, update)
        return _FRONTMATTER_RE.sub("", raw, count=1).strip()

    @staticmethod
    def _parse_entries(body: str) -> list[tuple[str, str, str]]:
        """Parse a typed file body into [(title, date, entry_body), ...].

        Entry format: ``## {title} ({YYYY-MM-DD})\\n{body}\\n`` separated by next ``## ``.
        """
        if not body.strip():
            return []
        entries = []
        pattern = re.compile(r"^## (.+?) \((\d{4}-\d{2}-\d{2})\)\s*$", re.MULTILINE)
        matches = list(pattern.finditer(body))
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            date = m.group(2)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            entry_body = body[start:end].strip()
            entries.append((title, date, entry_body))
        return entries

    # ── Actions ──────────────────────────────────────────────

    async def _action_read(self) -> str:
        _, body = await self._read_file("MEMORY")
        if not body:
            return "Memory index is empty."
        notice = memory_index_notice(body)
        return body + (f"\n\n[Index maintenance] {notice}" if notice else "")

    @staticmethod
    def _format_body(body: str, file_path: str) -> str:
        if len(body) <= _GET_BODY_MAX_CHARS:
            return body
        command = json.dumps(f"cat -- {shlex.quote(file_path)}", ensure_ascii=False)
        return (
            body[:_GET_BODY_MAX_CHARS]
            + f"\n\n... (Truncated: body length {len(body)} characters; returning the first {_GET_BODY_MAX_CHARS} characters. "
            f"Source path relative to workspace: {file_path}; read additional content with sandbox bash, "
            f'for example execute_code(language="bash", code={command}), or use sed to read selected lines.)'
        )

    async def _action_get(self, title: str, mem_type: str | None) -> str:
        title = title.strip()
        _, index_body = await self._read_file("MEMORY")
        matches = [
            entry
            for entry in parse_memory_index(index_body)
            if entry.title == title and (mem_type is None or entry.type == mem_type)
        ]
        if len(matches) > 1:
            return f"Multiple index entries have title='{title}' memories. Specify type or remove duplicate index entries."
        entry = matches[0] if matches else None
        if entry is not None and entry.file_path is not None:
            body = await ws_call(self._ws, "file_read", entry.file_path)
            if body is None:
                return f"Memory file '{entry.file_path}' does not exist; use execute_code to restore the file or repair the index."
            truncated = self._format_body(body, entry.file_path)
            return f"## {title} ({entry.date}) [type={entry.type}]\nFile: {entry.file_path}\n\n{truncated}"

        if mem_type is None:
            if entry is None:
                return f"No index entry found for title='{title}' memories. First read the index to verify the title."
            mem_type = entry.type

        if mem_type not in MEMORY_TYPES:
            return f"Unknown type: {mem_type}; valid values: {MEMORY_TYPES}"

        _, body = await self._read_file(f"MEMORY_{mem_type}")
        entries = self._parse_entries(body)
        for t, date, entry_body in entries:
            if t == title:
                truncated = self._format_body(entry_body, f"prompts/MEMORY_{mem_type}.md")
                return f"## {title} ({date}) [type={mem_type}]\n\n{truncated}"

        return f"MEMORY_{mem_type}.md has no section '## {title}'. The index may be stale; try rebuild_index."

    async def _action_register_file(
        self, mem_type: str, title: str, file_path: str, summary: str | None
    ) -> str:
        title = title.strip()
        if not title or not summary or not summary.strip():
            return "File registration requires nonempty title and summary."
        if mem_type not in MEMORY_TYPES:
            return f"Unknown type: {mem_type}; valid values: {MEMORY_TYPES}"
        file_path = file_path.replace("\\", "/")
        body = await ws_call(self._ws, "file_read", file_path)
        if body is None:
            return f"File '{file_path}' does not exist. Create it with execute_code before registration."
        if not body.strip():
            return "Memory file is empty. Write content before registering the file."
        _, typed_body = await self._read_file(f"MEMORY_{mem_type}")
        if any(t == title for t, _, _ in self._parse_entries(typed_body)):
            return f"[{mem_type}] an entry with this title already exists: '{title}'. Choose another title."
        summary = summary.strip().splitlines()[0][:_SUMMARY_MAX_CHARS]
        entry = MemoryIndexEntry(
            type=mem_type,
            title=title,
            date=datetime.now().strftime("%Y-%m-%d"),
            summary=summary,
            file_path=file_path,
        )
        index_line = entry.index_line()

        def append_index(index_body: str) -> str:
            entries = parse_memory_index(index_body)
            if any(entry.file_path == file_path for entry in entries):
                raise _MemoryConflict(
                    f"File '{file_path}' Registered; no duplicate index entry is needed. After body edits, get reads the latest content."
                )
            if any(entry.type == mem_type and entry.title == title for entry in entries):
                raise _MemoryConflict(
                    f"[{mem_type}] an entry with this title already exists: '{title}'. Choose another title."
                )
            return f"{index_body}\n{index_line}".strip()

        new_body = await self._update_file(
            "MEMORY", append_index, default_description="Long-term memory index"
        )
        notice = memory_index_notice(new_body)
        return f"Registered memory file and updated index: {index_line}" + (
            f"\n\n[Index maintenance] {notice}" if notice else ""
        )

    async def _action_append(self, mem_type: str, title: str, content: str, summary: str | None) -> str:
        if mem_type not in MEMORY_TYPES:
            return f"Unknown type: {mem_type}; valid values: {MEMORY_TYPES}"
        title = title.strip()
        content = content.strip()
        if not title or not content:
            return "title and content must not be empty."

        date = datetime.now().strftime("%Y-%m-%d")
        summary = (summary or content.splitlines()[0]).strip()
        summary = summary.splitlines()[0]
        if len(summary) > _SUMMARY_MAX_CHARS:
            summary = summary[:_SUMMARY_MAX_CHARS].rstrip() + "…"

        # 1) Append to typed file (dedup: if title already exists, reject)
        _, index_body = await self._read_file("MEMORY")
        existing = {entry.title for entry in parse_memory_index(index_body) if entry.type == mem_type}
        if title in existing:
            return (
                f"[{mem_type}] an entry with this title already exists '{title}'."
                "Edit independent files with execute_code; update grouped entries with rewrite. Use another title for a new topic."
            )
        new_entry = f"## {title} ({date})\n\n{content}\n"

        def append_typed(typed_body: str) -> str:
            if any(t == title for t, _, _ in self._parse_entries(typed_body)):
                raise _MemoryConflict(
                    f"[{mem_type}] an entry with this title already exists: '{title}'. Update it with rewrite or choose another title."
                )
            return (typed_body + "\n\n" + new_entry).strip()

        await self._update_file(
            f"MEMORY_{mem_type}",
            append_typed,
            default_description=f"{mem_type} type long-term memory entries",
        )

        # 2) Append a line to MEMORY.md index
        index_line = f"- [{mem_type}] {title} ({date}) — {summary}"

        def append_index(index_body: str) -> str:
            for entry in parse_memory_index(index_body):
                if entry.type == mem_type and entry.title == title:
                    if entry.file_path is not None:
                        raise _MemoryConflict(
                            f"[{mem_type}] an independent file with this title already exists,Choose another title and reconcile the grouped entry."
                        )
                    # A concurrent rebuild may already have indexed this section.
                    return index_body
            return (index_body + "\n" + index_line).strip()

        new_index_body = await self._update_file(
            "MEMORY", append_index, default_description="Long-term memory index"
        )

        logger.info(f"Appended memory [{mem_type}] '{title}' for user {self._user_id}")
        notice = memory_index_notice(new_index_body)
        return f"Wrote MEMORY_{mem_type}.md and updated index: {index_line}" + (
            f"\n\n[Index maintenance] {notice}" if notice else ""
        )

    async def _action_rewrite(self, mem_type: str, content: str) -> str:
        if mem_type not in MEMORY_TYPES:
            return f"Unknown type: {mem_type}; valid values: {MEMORY_TYPES}"
        content = content.strip()
        if not content:
            return "content must not be empty."

        _, expected_body = await self._read_file(f"MEMORY_{mem_type}")

        def replace_body(body: str) -> str:
            if body != expected_body:
                raise _MemoryConflict(
                    "Grouped memory was changed by another operation,reread the file before rewrite."
                )
            return content

        await self._update_file(
            f"MEMORY_{mem_type}",
            replace_body,
            default_description=f"{mem_type} type long-term memory entries",
        )
        notice = await self._action_rebuild_index(silent=True)

        logger.info(f"Rewrote MEMORY_{mem_type}.md for user {self._user_id}")
        return f"MEMORY_{mem_type}.md rewritten,index synchronized." + notice

    async def _action_rebuild_index(self, silent: bool = False) -> str:
        """Rebuild typed entries while retaining registered workspace-file links."""

        def rebuild(old_index: str) -> str:
            lines = [
                entry.index_line() for entry in parse_memory_index(old_index) if entry.file_path is not None
            ]
            for mem_type in MEMORY_TYPES:
                raw = self._ws.read_prompt(f"MEMORY_{mem_type}") or ""
                typed_body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
                for title, date, entry_body in self._parse_entries(typed_body):
                    summary = entry_body.splitlines()[0].strip() if entry_body else ""
                    if len(summary) > _SUMMARY_MAX_CHARS:
                        summary = summary[:_SUMMARY_MAX_CHARS].rstrip() + "…"
                    lines.append(f"- [{mem_type}] {title} ({date}) — {summary}")
            return "\n".join(lines)

        body = await self._update_file("MEMORY", rebuild, default_description="Long-term memory index")
        count = len(parse_memory_index(body))
        notice = memory_index_notice(body)
        suffix = f"\n\n[Index maintenance] {notice}" if notice else ""
        if silent:
            return suffix
        logger.info(f"Rebuilt memory index for user {self._user_id}: {count} entries")
        return f"Index rebuilt: {count} memories." + suffix


# Keep Python import compatibility; only the new names are exposed to the LLM.
UpdateMemoryTool = MemoryTool
UpdateMemoryParams = MemoryParams
SkillManagerTool = SkillTool
SkillManagerParams = SkillParams


# --- User Profile helpers ---

_FRONTMATTER_RE = re.compile(r"^(---\s*\n.*?\n---\s*\n)", re.DOTALL)

_BASIC_SINGLE = ["Name", "Nickname", "Timezone", "Workspace"]
_BASIC_MULTI = ["Social accounts"]
_TRADE_SINGLE = [
    "Main products",
    "Company name",
    "Company website",
    "Company scale",
    "Current stage/goals",
    "Main industry",
    "Target markets",
    "Platform preferences",
    "Style preferences",
    "Business strategy",
]
_GENERAL_MULTI = ["Background", "Preferences"]

_SINGLE_FIELDS = _BASIC_SINGLE + _TRADE_SINGLE
_MULTI_FIELDS = _BASIC_MULTI + _GENERAL_MULTI
_ALL_FIELDS = _SINGLE_FIELDS + _MULTI_FIELDS

# Append new comma-separated values and deduplicate them.
_LIST_FIELDS = {"Main products", "Target markets", "Platform preferences"}


def _parse_user_fields(body: str) -> dict[str, str]:
    """Parse USER.md body into {field_name: value} dict."""
    fields: dict[str, str] = {f: "" for f in _ALL_FIELDS}
    if not body.strip():
        return fields

    # Earlier templates used localized labels in the same fixed field order.
    # Preserve existing values when upgrading a complete legacy profile. Never
    # guess a partial/custom schema and silently discard unrecognized data.
    legacy_labels = re.compile(r"^[\u3400-\u9fff/]+[:\uFF1A]", re.MULTILINE)
    legacy_matches = list(legacy_labels.finditer(body))
    known_labels = sum(bool(re.search(rf"^{re.escape(label)}:", body, re.MULTILINE)) for label in _ALL_FIELDS)
    if legacy_matches and known_labels != len(_ALL_FIELDS):
        ordered_labels = _BASIC_SINGLE + _BASIC_MULTI + _TRADE_SINGLE + _GENERAL_MULTI
        if len(legacy_matches) != len(ordered_labels) or known_labels:
            raise ValueError(
                "Legacy profile has a partial or custom schema; migrate its labels before updating."
            )
        labels = iter(ordered_labels)
        body = legacy_labels.sub(lambda _match: next(labels) + ":", body)

    lines = body.split("\n")
    current_field: str | None = None
    current_lines: list[str] = []

    for line in lines:
        # Skip section headers and hint lines
        stripped = line.strip()
        if stripped.startswith("## ") or (stripped.startswith("(") and stripped.endswith(")")):
            # Save previous field before skipping
            if current_field is not None:
                fields[current_field] = "\n".join(current_lines).strip()
                current_field = None
                current_lines = []
            continue

        # Check if this line starts a new field
        matched = None
        for f in _ALL_FIELDS:
            if line.startswith(f"{f}:"):
                matched = f
                break

        if matched:
            # Save previous field
            if current_field is not None:
                fields[current_field] = "\n".join(current_lines).strip()

            current_field = matched
            if matched in _SINGLE_FIELDS:
                # Single-line: value is on the same line after "Field name:"
                value = line[len(f"{matched}:") :].strip()
                current_lines = [value] if value else []
            else:
                # Multi-line: value starts from next line
                current_lines = []
        else:
            if current_field is not None:
                current_lines.append(line)

    # Save last field
    if current_field is not None:
        fields[current_field] = "\n".join(current_lines).strip()

    return fields


async def _append_user_profile_fields(
    user_id: str, updates: dict[str, str], multi_field_append: bool = True, ws=None
) -> list[str]:
    """Append/Update fields to USER.md. Preserves existing fields not being updated."""
    from datetime import datetime

    ws = ws or Workspace(user_id)
    raw = await ws_call(ws, "read_prompt", "USER")
    if not raw:
        return []

    # Filter to valid fields only
    valid_updates = {k: v.strip() for k, v in updates.items() if k in _ALL_FIELDS}
    if not valid_updates:
        return []

    # Parse existing fields
    body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
    fields = _parse_user_fields(body)

    # Determine multi-line fields that need special handling
    multi_line_fields = set(_BASIC_MULTI + _GENERAL_MULTI)

    # Process each update
    for key, value in valid_updates.items():
        value = value.strip()
        if not value:
            continue  # Skip empty values

        if key in _LIST_FIELDS and multi_field_append:
            # Comma-separated list field: deduplicated append
            existing = fields.get(key, "").strip()
            if existing:
                existing_items = [i.strip() for i in re.split(r"[,\uFF0C\u3001]", existing) if i.strip()]
                new_items = [i.strip() for i in re.split(r"[,\uFF0C\u3001]", value) if i.strip()]
                seen = set(existing_items)
                for item in new_items:
                    if item not in seen:
                        existing_items.append(item)
                        seen.add(item)
                fields[key] = ", ".join(existing_items)
            else:
                fields[key] = value
        elif key in multi_line_fields and multi_field_append:
            # Multi-line field: append with separator and timestamp
            existing = fields.get(key, "").strip()
            if existing:
                timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
                fields[key] = existing + f"\n{timestamp} {value}"
            else:
                timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
                fields[key] = f"{timestamp} {value}"
        else:
            # Single-line field: update value
            fields[key] = value

    # Serialize back
    new_body = _serialize_user_fields(fields)

    # Preserve frontmatter and write back
    m = _FRONTMATTER_RE.match(raw)
    frontmatter = m.group(1) if m else ""
    await ws_call(ws, "write_prompt", "USER", frontmatter + "\n" + new_body + "\n")

    return list(valid_updates.keys())


def _serialize_user_fields(fields: dict[str, str]) -> str:
    """Serialize {field_name: value} dict back to USER.md body text with section headers."""
    parts: list[str] = []

    # Basic information
    parts.append("## Basic information")
    for f in _BASIC_SINGLE:
        value = fields.get(f, "")
        parts.append(f"{f}: {value}" if value else f"{f}:")
    for f in _BASIC_MULTI:
        value = fields.get(f, "")
        parts.append("")
        parts.append(f"{f}:")
        if value:
            parts.append(value)

    # Business information
    parts.append("")
    parts.append("")
    parts.append("## Business information")
    for f in _TRADE_SINGLE:
        value = fields.get(f, "")
        parts.append(f"{f}: {value}" if value else f"{f}:")

    # General information
    parts.append("")
    parts.append("")
    parts.append("## General information")
    for f in _GENERAL_MULTI:
        value = fields.get(f, "")
        parts.append(f"{f}:")
        if value:
            parts.append(value)
        parts.append("")

    return "\n".join(parts)


async def _update_user_profile_fields(user_id: str, updates: dict[str, str], ws=None) -> list[str]:
    """Update specific fields in USER.md. Returns list of updated field names."""
    ws = ws or Workspace(user_id)
    raw = await ws_call(ws, "read_prompt", "USER")
    if not raw:
        return []

    # Filter to valid fields only
    valid_updates = {k: v.strip() for k, v in updates.items() if k in _ALL_FIELDS}
    if not valid_updates:
        return []

    # Parse → merge → serialize
    body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
    fields = _parse_user_fields(body)
    for key, value in valid_updates.items():
        fields[key] = value
    new_body = _serialize_user_fields(fields)

    # Preserve frontmatter and write back
    m = _FRONTMATTER_RE.match(raw)
    frontmatter = m.group(1) if m else ""
    await ws_call(ws, "write_prompt", "USER", frontmatter + "\n" + new_body + "\n")

    return list(valid_updates.keys())


async def get_user_profile(user_id: str, ws=None) -> str:
    """Read the body of the user's USER.md (everything after frontmatter)."""
    ws = ws or Workspace(user_id)
    raw = await ws_call(ws, "read_prompt", "USER")
    if not raw:
        return ""

    return _FRONTMATTER_RE.sub("", raw, count=1).strip()


# --- Schedule Manager Tool ---


class ScheduleManagerParams(BaseModel):
    """Manage scheduled tasks."""

    action: Literal["create", "list", "delete"] = Field(
        description="Action: create=Create a scheduled task, list=List all tasks, delete=Delete task"
    )
    name: str | None = Field(default=None, description="Task name; required for create")
    skill: str | None = Field(default=None, description="Associated skill name(optional)")
    skill_desc: str | None = Field(default=None, description="Short skill description(optional)")
    prompt: str | None = Field(
        default=None, description="Prompt sent to the agent at the scheduled time; required for create"
    )
    hour: int | None = Field(default=8, description="Execution time-hour(0-23,default 8)")
    minute: int | None = Field(default=0, description="Execution time-minutes(0-59,default 0)")
    repeat: str | None = Field(
        default="daily", description="Repeat schedule: daily=Daily, weekly=Weekly, once=Once"
    )
    task_id: str | None = Field(default=None, description="Task ID; required for delete")


class ScheduleManagerTool(BaseTool):
    name: str = "schedule_manager"
    description: str = (
        "Create, view or delete scheduled tasks for recurring automation.\n"
        "- create: Create a scheduled task(requires name, prompt, hour, minute)\n"
        "- list: List current scheduled tasks\n"
        "- delete: Delete a task(requires task_id)\n"
        "Created tasks are sent to the interface, which triggers execution at the scheduled time."
    )
    param_anno = ScheduleManagerParams

    def __init__(self, user_id: str, workspace: "Workspace" = None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._ws = workspace or Workspace(user_id)

    async def __call__(self, params: ScheduleManagerParams) -> str:
        import time

        if params.action == "create":
            if not params.name or not params.prompt:
                return "Creating a task requires name and prompt parameters."
            task = {
                "id": f"t_{int(time.time())}",
                "name": params.name,
                "skill": params.skill or "",
                "skill_desc": params.skill_desc or "",
                "prompt": params.prompt,
                "hour": params.hour if params.hour is not None else 8,
                "minute": params.minute if params.minute is not None else 0,
                "repeat": params.repeat or "daily",
                "enabled": True,
            }
            # Return JSON so agent.py can detect and emit code 7
            return json.dumps({"action": "create", "task": task}, ensure_ascii=False)

        elif params.action == "list":
            return json.dumps({"action": "list"}, ensure_ascii=False)

        elif params.action == "delete":
            if not params.task_id:
                return "Deleting a task requires task_id parameters."
            return json.dumps({"action": "delete", "task_id": params.task_id}, ensure_ascii=False)

        return f"Unknown action: {params.action}"


# --- Parent History Tool (for sub-agents) ---


class ParentHistoryParams(BaseModel):
    """Query the parent agent's conversation history (sub-agent use only)."""

    action: Literal["tail", "full", "search", "last_user"] = Field(
        description=(
            "tail: latest N messages;"
            "full: Full history(truncated to the length limit);"
            "search: Match keyword as a case-insensitive substring;"
            "last_user: Return only the latest user message"
        ),
    )
    n: int = Field(
        default=5,
        description="action=tail number of messages to return(default 5,limit 30)",
    )
    keyword: str = Field(
        default="",
        description="action=search search keyword",
    )


_PARENT_HIST_MAX_CHARS = 15000
_PARENT_HIST_MAX_MSGS = 50
_PARENT_HIST_PER_MSG_CHARS = 800


class ParentHistoryTool(BaseTool):
    """Let a sub-agent read its parent agent's conversation history on demand.

    Only instantiated when a sub-agent is spawned by a parent (via
    ``SubAgentTool`` or Phase 2.5 handoff). Sub-agents invoked directly
    through ``POST /api/sub-agent/{type}/chat`` do not have this tool.
    """

    name: str = "parent_history"
    description: str = (
        "Inspect parent agent conversation history. The parent may omit background when starting a subtask. "
        "Read original user messages, previous tool calls, URLs, code or error messages when needed.\n"
        "- action=tail, n=5:latest 5 messages\n"
        "- action=full:Full history(truncated to the length limit)\n"
        "- action=search, keyword=xxx:Find messages containing xxx\n"
        "- action=last_user:Latest user message(useful for checking current intent)"
    )
    param_anno = ParentHistoryParams

    def __init__(self, parent_history_fn, **kwargs):
        super().__init__(**kwargs)
        # Callable returning the *live* parent history list; must not be None.
        self._fn = parent_history_fn

    async def __call__(self, params: ParentHistoryParams) -> str:
        try:
            history = list(self._fn() or [])
        except Exception as e:
            logger.warning(f"ParentHistoryTool fetch failed: {e}")
            return f"Cannot read parent agent History: {e}"

        if not history:
            return "Parent agent history is empty."

        if params.action == "tail":
            n = max(1, min(int(params.n or 5), 30))
            return self._format(history[-n:])

        if params.action == "full":
            return self._format(history)

        if params.action == "search":
            kw = (params.keyword or "").strip().lower()
            if not kw:
                return "search requires keyword."
            matches = [m for m in history if kw in str(m.get("content", "")).lower()]
            if not matches:
                return f"No messages contain {kw!r} messages."
            return self._format(matches)

        if params.action == "last_user":
            for m in reversed(history):
                if m.get("role") == "user":
                    return str(m.get("content", ""))[:_PARENT_HIST_MAX_CHARS]
            return "Parent agent history has no user messages."

        return f"Unknown action: {params.action}"

    @staticmethod
    def _format(messages: list) -> str:
        if not messages:
            return "(No matching messages)"
        # Keep only the most recent N to bound token cost
        tail = messages[-_PARENT_HIST_MAX_MSGS:]
        lines = []
        for m in tail:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if content is None:
                content = "[Tool call]"
            text = str(content)[:_PARENT_HIST_PER_MSG_CHARS]
            lines.append(f"[{role}] {text}")
        result = "\n\n".join(lines)
        if len(result) > _PARENT_HIST_MAX_CHARS:
            result = result[:_PARENT_HIST_MAX_CHARS] + "\n... [history truncated]"
        header = f"Parent agent history ({len(messages)} messages total, showing {len(tail)}):\n\n"
        return header + result


# --- Sub-Agent Tool ---


class SubAgentParams(BaseModel):
    """Parameters for invoking a sub-agent."""

    task: str = Field(description="Task for the sub-agent")
    agent_type: str = Field(
        description="Sub-agent type matching a .md filename under configs/agents/",
    )
    context: str | None = Field(
        default=None,
        description="Additional context for the sub-agent",
    )


class SubAgentTool(BaseTool):
    """Invoke a sub-agent to execute an independent task.

    SubAgentRunner runs inside __call__, collecting text output for SDK return.
    Sub-agent's tool events (code 4/5/9/10) are stored in _sub_events for
    _stream_sdk_events to flush before emitting the tool result.
    """

    name: str = "sub_agent"
    description: str = "Delegate an independent task to a sub-agent."
    param_anno = SubAgentParams

    def __init__(self, user_id: str = "", workspace=None, parent_history_fn=None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._workspace = workspace
        # Callable returning the live parent-agent history. When set, the
        # sub-agent will get a `parent_history` tool it can use on demand.
        self._parent_history_fn = parent_history_fn
        self._sub_events: list[dict] = []
        # Dynamically build description from available agent types
        try:
            from core.sub_agent import list_agent_types

            agents = list_agent_types()
            if agents:
                lines = "\n".join(f"- {a.name}: {a.description}" for a in agents)
                self.description = f"Delegate an independent task to a sub-agent.Available types:\n{lines}"
        except Exception:
            pass

    async def __call__(self, params: SubAgentParams) -> str:
        """Run sub-agent, collect text output, buffer tool events."""
        from core.sub_agent import SubAgentRunner

        self._sub_events.clear()

        try:
            runner = SubAgentRunner(
                agent_type=params.agent_type,
                user_id=self._user_id,
                session_id=f"from_main_{uuid4().hex[:8]}",
                workspace=self._workspace,
                parent_history_fn=self._parent_history_fn,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        final_text = ""
        try:
            async for event in runner.run_stream(params.task, params.context or ""):
                code = event.get("code")
                content = event.get("content", "")

                if code == 2:
                    final_text += content
                elif code in (4, 5, 9, 10):
                    self._sub_events.append(event)

        except Exception as e:
            logger.error(f"SubAgentTool error: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        if "[SKILL_PROPOSAL]" in final_text:
            self._sub_events.append({"code": 12, "content": final_text})
            report = re.sub(r"\[SKILL_PROPOSAL\][\s\S]*?\[/SKILL_PROPOSAL\]", "", final_text).strip()
            if "[SKILL_PROPOSAL]" in report:
                return "Sub-agent skill proposal output is incomplete. A retry notice was shown to the user; do not save incomplete proposals."
            return (
                report[:3000]
                + "\n\nThe complete skill proposal was sent to the user confirmation card. Wait for user review and confirmation; do not repeat the proposal or reference bodies."
            ).strip()
        return final_text[:4000] if final_text else "Sub-agent execution completed"
