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

# Reverse mapping: Chinese field name → English param name
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

    # Convert Chinese field names → English param names
    result = {}
    for cn_name, value in fields.items():
        eng_name = _FIELD_TO_PARAM.get(cn_name)
        if eng_name:
            result[eng_name] = value
    return {"success": True, "data": result}


@router.put("/profile")
async def update_profile(request: ProfileUpdateRequest):
    """Update user profile fields (only provided fields are updated)."""
    # Map English params → Chinese field names, skip None
    updates = {}
    for eng, cn in _PARAM_TO_FIELD.items():
        val = getattr(request, eng, None)
        if val is not None:
            updates[cn] = val

    if not updates:
        return {"success": True, "updated": []}

    updated = _append_user_profile_fields(request.user_id, updates, multi_field_append=True)
    return {
        "success": True,
        "updated": [_FIELD_TO_PARAM.get(cn, cn) for cn in updated],
    }
