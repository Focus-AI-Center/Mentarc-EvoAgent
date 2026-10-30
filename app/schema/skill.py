"""Skill data models for API layer."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from utils.skill_references import normalize_references


class SkillReferenceFields(BaseModel):
    references: dict[str, str] = Field(default_factory=dict)

    @field_validator("references")
    @classmethod
    def validate_references(cls, value):
        return normalize_references(value)


class SkillParam(BaseModel):
    name: str
    description: str
    default: str | None = None
    example: str


class SkillCreateRequest(SkillReferenceFields):
    name: str
    description: str
    triggers: list[str]
    instructions: str
    required_tools: list[str] = []
    parameters: list[SkillParam] = []
    tags: list[str] = []
    source: Literal["user_defined", "extracted"] = "user_defined"
    extracted_from: str | None = None


class SkillUpdateRequest(BaseModel):
    description: str | None = None
    triggers: list[str] | None = None
    instructions: str | None = None
    required_tools: list[str] | None = None
    parameters: list[SkillParam] | None = None
    tags: list[str] | None = None
    references: dict[str, str] | None = None

    @field_validator("references")
    @classmethod
    def validate_references(cls, value):
        return normalize_references(value) if value is not None else None
