"""Resolve named agent definitions from one per-request workspace catalog."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from configs.settings import DEFAULT_AGENTS_DIR
from utils.logger import logger


class AgentTypeConfig(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    model: str | None = None
    max_steps: int = Field(default=30, ge=1)
    timeout: float = Field(default=300, gt=0)
    execution_mode: Literal["interactive", "worker"] = "interactive"
    body: str = ""


def parse_agent_config(name: str, raw: str) -> AgentTypeConfig:
    metadata, body = {}, raw
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ValueError(f"Agent '{name}' has unclosed frontmatter.")
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
        body = "\n".join(lines[end + 1 :])
    if not isinstance(metadata, dict) or metadata.get("name", name) != name:
        raise ValueError(f"Agent '{name}' must use its filename as its name.")
    if not body.strip():
        raise ValueError(f"Agent '{name}' has no instructions.")
    return AgentTypeConfig(**(metadata | {"name": name, "body": body.strip()}))


def load_agent_config(agent_type: str, workspace=None) -> AgentTypeConfig:
    # Validate before constructing either a local path or an object key.
    AgentTypeConfig(name=agent_type)
    raw = workspace.agent_get(agent_type) if workspace is not None else None
    if raw is None:
        path = Path(DEFAULT_AGENTS_DIR) / f"{agent_type}.md"
        if not path.is_file():
            raise ValueError(f"Agent type '{agent_type}' not found.")
        raw = path.read_text(encoding="utf-8")
    return parse_agent_config(agent_type, raw)


def agent_catalog(workspace=None) -> dict[str, AgentTypeConfig]:
    """Workspace definitions shadow defaults; invalid overrides never revive a default."""
    definitions = {}
    for path in sorted(Path(DEFAULT_AGENTS_DIR).glob("*.md")):
        try:
            definitions[path.stem] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("Unreadable default agent definition skipped: {}", path.name)
    if workspace is not None:
        definitions.update(workspace.agent_get_all())
    result = {}
    for name, raw in sorted(definitions.items()):
        try:
            result[name] = parse_agent_config(name, raw)
        except (ValueError, yaml.YAMLError):
            logger.warning("Invalid agent definition skipped: {}", name)
    return result


def list_agent_types(workspace=None) -> list[AgentTypeConfig]:
    return list(agent_catalog(workspace).values())


def match_agent(user_input: str, workspace=None) -> AgentTypeConfig | None:
    return next(
        (
            config
            for config in list_agent_types(workspace)
            if any(trigger.lower() in user_input.lower() for trigger in config.triggers)
        ),
        None,
    )
