"""Skill management API router."""

import asyncio

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
    skills = await asyncio.to_thread(manager.get_all_skills, user_id)
    return {
        "skills": [s.model_dump() for s in skills.values()],
        "count": len(skills),
    }


@router.post("/{user_id}/create")
async def create_skill(user_id: str, request: SkillCreateRequest):
    """Manually create a new skill."""
    manager = _get_manager()
    if await asyncio.to_thread(manager.skill_exists, user_id, request.name):
        raise HTTPException(409, "This skill already exists. Confirm an update instead of creating it again.")
    skill = Skill(
        name=request.name,
        description=request.description,
        triggers=request.triggers,
        instructions=request.instructions,
        required_tools=request.required_tools,
        parameters=[parameter.model_dump() for parameter in request.parameters],
        tags=request.tags,
        references=request.references,
        source=request.source,
        extracted_from=request.extracted_from,
        status="confirmed",
        created_by=user_id,
    )
    await asyncio.to_thread(manager.save_user_skill, user_id, skill)
    return {"status": "created", "skill": skill.model_dump()}


@router.put("/{user_id}/{skill_name:path}")
async def update_skill(user_id: str, skill_name: str, request: SkillUpdateRequest):
    """Update an existing skill."""
    manager = _get_manager()
    updates = request.model_dump(exclude_none=True)
    skill = await asyncio.to_thread(manager.update_user_skill, user_id, skill_name, updates)
    if not skill:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    return {"status": "updated", "skill": skill.model_dump()}


@router.delete("/{user_id}/{skill_name:path}")
async def delete_skill(user_id: str, skill_name: str):
    """Delete a skill."""
    manager = _get_manager()
    success = await asyncio.to_thread(manager.delete_user_skill, user_id, skill_name)
    if not success:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    return {"status": "deleted", "skill_name": skill_name}
