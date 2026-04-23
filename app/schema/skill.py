"""Skill data models for API layer."""

from pydantic import BaseModel


class SkillParam(BaseModel):
    name: str
    description: str
    default: str | None = None
    example: str


class SkillCreateRequest(BaseModel):
    name: str
    description: str
    triggers: list[str]
    instructions: str
    required_tools: list[str] = []
    parameters: list[SkillParam] = []
    tags: list[str] = []


class SkillUpdateRequest(BaseModel):
    description: str | None = None
    triggers: list[str] | None = None
    instructions: str | None = None
    required_tools: list[str] | None = None
    parameters: list[SkillParam] | None = None
    tags: list[str] | None = None
