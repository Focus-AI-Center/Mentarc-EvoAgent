"""Skill-related tools that the agent can use during conversations."""

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.skills.manager import SkillManager


class ListSkillsParams(BaseModel):
    query: str = Field(default="", description="Optional: filter skills by keyword")


class ListSkillsTool(BaseTool):
    """Tool for the agent to list available skills."""

    name: str = "list_skills"
    description: str = "List all available skills. Optionally filter by keyword."
    param_anno = ListSkillsParams

    def __init__(self, skill_manager: SkillManager = None, user_id: str = "", **kwargs):
        super().__init__(**kwargs)
        self._skill_manager = skill_manager or SkillManager()
        self._user_id = user_id

    async def __call__(self, query: str = "", **kwargs) -> str:
        skills = self._skill_manager.get_all_skills(self._user_id)
        if not skills:
            return "No skills available."

        if query:
            query_lower = query.lower()
            skills = {
                k: v
                for k, v in skills.items()
                if query_lower in v.name.lower()
                or query_lower in v.description.lower()
                or any(query_lower in t.lower() for t in v.tags)
            }

        if not skills:
            return f"No skills found matching '{query}'."

        lines = []
        for skill in skills.values():
            triggers = ", ".join(skill.triggers[:3])
            lines.append(f"- **{skill.name}**: {skill.description} (触发词: {triggers})")
        return "\n".join(lines)


class ActivateSkillParams(BaseModel):
    skill_name: str = Field(description="The name of the skill to activate")


class ActivateSkillTool(BaseTool):
    """Tool for the agent to explicitly activate a skill's instructions."""

    name: str = "activate_skill"
    description: str = "Activate a skill to load its detailed instructions and workflow."
    param_anno = ActivateSkillParams

    def __init__(self, skill_manager: SkillManager = None, user_id: str = "", **kwargs):
        super().__init__(**kwargs)
        self._skill_manager = skill_manager or SkillManager()
        self._user_id = user_id

    async def __call__(self, skill_name: str, **kwargs) -> str:
        skills = self._skill_manager.get_all_skills(self._user_id)
        skill = skills.get(skill_name)
        if not skill:
            return f"Skill '{skill_name}' not found."

        # Increment usage
        self._skill_manager.increment_usage(self._user_id, skill_name)

        result = f"## 技能: {skill.name}\n\n{skill.instructions}"
        if skill.parameters:
            params = "\n".join(
                f"- **{p.name}**: {p.description} (示例: {p.example})" for p in skill.parameters
            )
            result += f"\n\n## 参数\n{params}"

        return result
