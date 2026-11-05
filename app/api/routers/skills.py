"""Skill management API router."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schema.skill import SkillCreateRequest, SkillUpdateRequest
from core.skills import Skill, proposals
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


# Proposal routes must be declared before the catch-all ``/{user_id}/{skill_name:path}`` ones.
@router.get("/{user_id}/proposals")
async def list_proposals(user_id: str):
    """Changes proposed by an agent that are waiting for the user's decision."""
    records = await asyncio.to_thread(proposals.list_pending, _get_manager(), user_id)
    return {"proposals": [proposals.proposal_event(record) for record in records], "count": len(records)}


@router.post("/{user_id}/proposals/{proposal_id}/confirm")
async def confirm_proposal(user_id: str, proposal_id: str):
    """Apply a staged proposal. The content comes from storage, not from the request."""
    try:
        record, skill = await asyncio.to_thread(proposals.commit, _get_manager(), user_id, proposal_id)
    except proposals.ProposalNotFound:
        raise HTTPException(404, "This proposal does not exist or has expired.") from None
    except proposals.ProposalConflict as err:
        raise HTTPException(409, str(err)) from None
    return {
        "status": "confirmed",
        "operation": record["operation"],
        "name": record["name"],
        "skill": skill.model_dump() if skill else None,
    }


@router.delete("/{user_id}/proposals/{proposal_id}")
async def reject_proposal(user_id: str, proposal_id: str):
    """Discard a staged proposal."""
    try:
        await asyncio.to_thread(proposals.reject, _get_manager(), user_id, proposal_id)
    except proposals.ProposalNotFound:
        raise HTTPException(404, "This proposal does not exist or has expired.") from None
    return {"status": "rejected", "proposal_id": proposal_id}


class UndoRequest(BaseModel):
    name: str


@router.post("/{user_id}/undo")
async def undo_skill_change(user_id: str, request: UndoRequest):
    """Restore the version saved before the skill's last change (also restores a deleted skill)."""
    try:
        skill = await asyncio.to_thread(_get_manager().undo_last_change, user_id, request.name)
    except LookupError as err:
        raise HTTPException(404, str(err)) from None
    return {"status": "restored", "skill": skill.model_dump()}


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
