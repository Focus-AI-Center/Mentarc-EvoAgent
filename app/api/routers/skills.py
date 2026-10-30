"""Skill management API router."""

from fastapi import APIRouter, HTTPException

from app.schema.skill import SkillCreateRequest, SkillUpdateRequest
from core.skills import Skill
from core.skills.manager import SkillManager

router = APIRouter()


def _get_manager() -> SkillManager:
    return SkillManager()


@router.get("/{user_id}")
async def list_skills(user_id: str):
    """List all skills for a user (builtin + user-defined + extracted)."""
    manager = _get_manager()
    skills = manager.get_all_skills(user_id)
    return {
        "skills": [s.model_dump() for s in skills.values()],
        "count": len(skills),
    }


@router.post("/{user_id}/create")
async def create_skill(user_id: str, request: SkillCreateRequest):
    """Manually create a new skill."""
    manager = _get_manager()
    skill = Skill(
        name=request.name,
        description=request.description,
        triggers=request.triggers,
        instructions=request.instructions,
        required_tools=request.required_tools,
        parameters=request.parameters,
        tags=request.tags,
        references=request.references,
        source=request.source,
        extracted_from=request.extracted_from,
        status="confirmed",
        created_by=user_id,
    )
    manager.save_user_skill(user_id, skill)
    return {"status": "created", "skill": skill.model_dump()}


@router.put("/{user_id}/{skill_name}")
async def update_skill(user_id: str, skill_name: str, request: SkillUpdateRequest):
    """Update an existing skill."""
    manager = _get_manager()
    updates = request.model_dump(exclude_none=True)
    skill = manager.update_user_skill(user_id, skill_name, updates)
    if not skill:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    return {"status": "updated", "skill": skill.model_dump()}


@router.delete("/{user_id}/{skill_name}")
async def delete_skill(user_id: str, skill_name: str):
    """Delete a skill."""
    manager = _get_manager()
    success = manager.delete_user_skill(user_id, skill_name)
    if not success:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    return {"status": "deleted", "skill_name": skill_name}
