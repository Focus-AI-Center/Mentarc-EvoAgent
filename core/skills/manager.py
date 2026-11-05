"""Skill manager - CRUD operations for skills using file-based workspace."""

from datetime import UTC, datetime

from core.skills import Skill, skill_from_md, skill_to_md
from utils.logger import logger
from utils.skill_references import normalize_references, with_reference_links
from utils.workspace import Workspace


class SkillManager:
    """Manages user-defined and extracted skills (stored as .md in workspace)."""

    def __init__(self, workspace: Workspace = None):
        self._workspace = workspace

    def _ws(self, user_id: str) -> Workspace:
        if self._workspace is not None:
            return self._workspace
        return Workspace(user_id)

    # --- previous version (one level, enough for undo) ---

    @staticmethod
    def _snapshot(ws: Workspace, name: str) -> dict | None:
        raw = ws.skill_get(name)
        if raw is None:
            return None
        return {
            "saved_at": datetime.now(UTC).isoformat(),
            "skill_md": raw,
            "refs": ws.skill_refs_get(name),
        }

    def _backup(self, ws: Workspace, name: str) -> None:
        snapshot = self._snapshot(ws, name)
        if snapshot is not None:
            ws.skill_prev_set(name, snapshot)

    def undo_last_change(self, user_id: str, name: str) -> Skill:
        """Restore the version saved before the last change.

        The state being replaced becomes the new previous version, so an undo can
        itself be undone. Also restores a skill that was deleted.
        """
        ws = self._ws(user_id)
        previous = ws.skill_prev_get(name)
        if not previous or not isinstance(previous.get("skill_md"), str):
            raise LookupError(f"Skill '{name}' has no previous version")
        current = self._snapshot(ws, name)
        if current is not None:
            ws.skill_prev_set(name, current)
        else:
            ws.skill_prev_del(name)
        wanted = previous.get("refs") or {}
        for filename in ws.skill_refs_get(name):
            if filename not in wanted:
                ws.skill_ref_del(name, filename)
        for filename, content in wanted.items():
            ws.skill_ref_set(name, filename, content)
        ws.skill_set(name, previous["skill_md"])  # main document last
        skill = skill_from_md(previous["skill_md"], fallback_name=name)
        skill.references = dict(wanted)
        return skill

    def get_user_skills(self, user_id: str) -> dict[str, Skill]:
        """Load user-defined/extracted skills from workspace."""
        ws = self._ws(user_id)
        raw_all = ws.skill_get_all()

        skills = {}
        for name, raw in raw_all.items():
            try:
                skill = skill_from_md(raw, fallback_name=name)
                refs = ws.skill_refs_get(name)
                if refs:
                    skill.references = refs
                skills[name] = skill
            except Exception as e:
                logger.warning(f"Failed to parse user skill '{name}': {e}")
        return skills

    def get_all_skills(self, user_id: str, top_level_only: bool = False) -> dict[str, Skill]:
        """Get all confirmed skills for a user.

        Args:
            top_level_only: If True, only return top-level skills (no '/' in name).
                           Sub-skills can still be loaded individually via get.
        """
        skills = self.get_user_skills(user_id)
        confirmed = {k: v for k, v in skills.items() if v.status == "confirmed"}
        if top_level_only:
            return {k: v for k, v in confirmed.items() if "/" not in k}
        return confirmed

    # --- CRUD ---

    def save_user_skill(self, user_id: str, skill: Skill) -> None:
        """Create or replace supplied content, preserving an existing skill's lifecycle."""
        references = normalize_references(skill.references)
        ws = self._ws(user_id)
        raw = ws.skill_get(skill.name)
        if raw:
            previous = skill_from_md(raw, fallback_name=skill.name)
            protected = {
                "name",
                "created_at",
                "created_by",
                "usage_count",
                "version",
                "success_rate",
                "confidence",
                "extracted_from",
            }
            updates = skill.model_dump(include=skill.model_fields_set - protected)
            updates.pop("references", None)
            merged = Skill.model_validate(previous.model_dump() | updates)
            merged.version = previous.version + 1
            merged.updated_at = datetime.now()
            merged.references = references
            # Keep the caller's object consistent with the stored representation.
            for key in Skill.model_fields:
                setattr(skill, key, getattr(merged, key))
        else:
            skill.created_by = user_id
        skill.references = references
        skill.instructions = with_reference_links(skill.instructions, references)
        skill.is_builtin = False
        if raw:
            self._backup(ws, skill.name)
        for filename, content in references.items():
            ws.skill_ref_set(skill.name, filename, content)
        ws.skill_set(skill.name, skill_to_md(skill))

    def skill_exists(self, user_id: str, name: str) -> bool:
        return self._ws(user_id).skill_get(name) is not None

    def delete_user_skill(self, user_id: str, skill_name: str) -> bool:
        """Delete a user skill, keeping its last version so the deletion can be undone."""
        ws = self._ws(user_id)
        self._backup(ws, skill_name)
        return ws.skill_del(skill_name)

    def patch_user_skill(
        self, user_id: str, skill_name: str, old_string: str, new_string: str, ref_filename: str = ""
    ) -> None:
        """Apply one exact text edit; keep the pre-edit version only if the edit succeeds."""
        ws = self._ws(user_id)
        snapshot = self._snapshot(ws, skill_name)
        ws.skill_patch(skill_name, old_string, new_string, ref_filename)
        if snapshot is not None:
            ws.skill_prev_set(skill_name, snapshot)

    def update_user_skill(self, user_id: str, skill_name: str, updates: dict) -> Skill | None:
        """Update fields of an existing user skill."""
        skills = self.get_user_skills(user_id)
        skill = skills.get(skill_name)
        if not skill:
            return None

        for key, value in updates.items():
            if value is not None and hasattr(skill, key):
                if key == "references":
                    skill.references.update(normalize_references(value))
                else:
                    setattr(skill, key, value)
        skill.updated_at = datetime.now()
        self.save_user_skill(user_id, skill)
        return skill

    def increment_usage(self, user_id: str, skill_name: str) -> None:
        """Increment usage count for a skill."""
        ws = self._ws(user_id)
        raw = ws.skill_get(skill_name)
        if not raw:
            return
        skill = skill_from_md(raw, fallback_name=skill_name)
        skill.usage_count += 1
        skill.updated_at = datetime.now()
        ws.skill_set(skill_name, skill_to_md(skill))
