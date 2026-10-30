"""User profile API — structured JSON read/write for user profiles."""

from fastapi import APIRouter
from pydantic import BaseModel

from core.plugins.internal import (
    _FRONTMATTER_RE,
    _PARAM_TO_FIELD,
    _append_user_profile_fields,
    _parse_user_fields,
)
from utils.workspace import Workspace

router = APIRouter()

# Reverse mapping: profile field label -> tool parameter.
_FIELD_TO_PARAM = {v: k for k, v in _PARAM_TO_FIELD.items()}


class ProfileGetRequest(BaseModel):
    user_id: str


class ProfileUpdateRequest(BaseModel):
    user_id: str
    name: str | None = None
    nickname: str | None = None
    timezone: str | None = None
    workspace: str | None = None
    social_accounts: str | None = None
    main_products: str | None = None
    company_name: str | None = None
    company_website: str | None = None
    company_scale: str | None = None
    current_stage: str | None = None
    main_industry: str | None = None
    target_markets: str | None = None
    platform_preference: str | None = None
    style_preference: str | None = None
    business_strategy: str | None = None
    background: str | None = None
    preferences: str | None = None


@router.post("/profile")
async def get_profile(request: ProfileGetRequest):
    """Get user profile as structured JSON."""
    ws = Workspace(request.user_id)
    raw = ws.read_prompt("USER")
    body = _FRONTMATTER_RE.sub("", raw, count=1).strip() if raw else ""
    fields = _parse_user_fields(body)

    # Convert profile labels to public API parameter names.
    result = {}
    for label, value in fields.items():
        parameter = _FIELD_TO_PARAM.get(label)
        if parameter:
            result[parameter] = value
    return {"success": True, "data": result}


@router.put("/profile")
async def update_profile(request: ProfileUpdateRequest):
    """Update user profile fields (only provided fields are updated)."""
    # Map tool parameters to profile labels, skipping omitted values.
    updates = {}
    for parameter, label in _PARAM_TO_FIELD.items():
        val = getattr(request, parameter, None)
        if val is not None:
            updates[label] = val

    if not updates:
        return {"success": True, "updated": []}

    updated = await _append_user_profile_fields(request.user_id, updates, multi_field_append=True)
    return {
        "success": True,
        "updated": [_FIELD_TO_PARAM.get(label, label) for label in updated],
    }
