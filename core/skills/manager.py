"""Skill manager - CRUD operations for skills using file-based workspace."""

from datetime import datetime

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
        """Save a skill to the user's skill set."""
        references = normalize_references(skill.references)
        skill.references = references
        skill.instructions = with_reference_links(skill.instructions, references)
        skill.is_builtin = False
        skill.created_by = user_id
        ws = self._ws(user_id)
        for filename, content in references.items():
            ws.skill_ref_set(skill.name, filename, content)
        ws.skill_set(skill.name, skill_to_md(skill))

    def delete_user_skill(self, user_id: str, skill_name: str) -> bool:
        """Delete a user skill."""
        return self._ws(user_id).skill_del(skill_name)

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
