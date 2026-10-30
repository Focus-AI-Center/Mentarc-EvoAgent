"""Skill engine data models and MD serialization.

Skills are stored as .md files with YAML frontmatter (metadata)
and markdown body (instructions), consistent with how prompts work:

    ---
    name: write-report
    description: Generate an SEO report from keywords
    triggers: [write report, generate report]
    required_tools: [websearch, fetchurl]
    tags: [writing, seo]
    source: extracted
    version: 1
    usage_count: 5
    ---

    1. Search keyword-related sources with websearch
    2. Organize findings and write the report
    ...
"""

import re
from datetime import datetime
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from utils.skill_references import normalize_references
from utils.tool_names import canonical_tool_name, normalize_tool_references


class SkillParam(BaseModel):
    """A parameterizable variable in a skill."""

    name: str
    description: str
    default: str | None = None
    example: str


class Skill(BaseModel):
    """A reusable task pattern that the agent can execute."""

    name: str
    description: str
    triggers: list[str] = []
    instructions: str = ""
    required_tools: list[str] = []

    # Source & lifecycle
    source: Literal["builtin", "extracted", "user_defined"] = "builtin"
    status: Literal["confirmed", "archived"] = "confirmed"
    confidence: float = 1.0
    usage_count: int = 0
    success_rate: float = 1.0

    # Parameterization
    parameters: list[SkillParam] = []
    example_io: dict | None = None

    # Metadata
    extracted_from: str | None = None  # source session_id
    version: int = 1
    tags: list[str] = []
    is_builtin: bool = False
    created_by: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # Update tracking: when this candidate is an update to an existing skill
    update_target: str | None = None  # name of the existing skill to update

    # References (lazy-loaded content like templates)
    references: dict[str, str] = Field(default_factory=dict)

    @field_validator("references")
    @classmethod
    def validate_references(cls, value):
        return normalize_references(value)

    # Trigger-based sub-agent handoff (paper Appendix A, Phase 2.5).
    # When set, matching this skill bypasses the main agent: a dedicated
    # sub-agent is spawned with `sub_agent_instructions` + `sub_agent_tools`
    # and its result is surfaced directly to the user.
    requires_sub_agent: bool = False
    sub_agent_name: str | None = None
    sub_agent_instructions: str | None = None
    sub_agent_tools: list[str] = []


class SkillMatchResult(BaseModel):
    """Result of skill matching."""

    skill: Skill | None = None
    match_type: Literal["keyword", "embedding", "llm", "none"] = "none"
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# MD ↔ Skill serialization
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def skill_to_md(skill: Skill) -> str:
    """Serialize a Skill to markdown with YAML frontmatter.

    Metadata goes in frontmatter, instructions become the body.
    """
    data = skill.model_dump(exclude={"instructions", "references"}, mode="json")
    # Drop None / empty-default fields to keep YAML clean
    data = {k: v for k, v in data.items() if v is not None and v != [] and v != {}}
    yaml_str = yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    body = skill.instructions.strip()
    return f"---\n{yaml_str}---\n\n{body}\n"


def skill_from_md(content: str, fallback_name: str = "") -> Skill:
    """Parse a markdown file with YAML frontmatter into a Skill.

    Frontmatter → metadata fields, body → instructions.
    """
    match = _FRONTMATTER_RE.match(content)
    if match:
        yaml_str, body = match.groups()
        meta = yaml.safe_load(yaml_str) or {}
    else:
        meta = {}
        body = content

    meta["instructions"] = normalize_tool_references(body.strip())
    if isinstance(meta.get("required_tools"), list):
        meta["required_tools"] = [canonical_tool_name(name) for name in meta["required_tools"]]
    if fallback_name and "name" not in meta:
        meta["name"] = fallback_name

    return Skill.model_validate(meta)
