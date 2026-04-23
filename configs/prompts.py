"""Prompt loader - loads prompt templates from markdown files.

Each prompt is stored as a .md file with:
- YAML frontmatter: name, description, variables list
- Markdown body: the actual prompt template (with {variable} placeholders)

configs/prompts/ contains default templates. When a new user workspace is
created, these are copied into {workspace}/prompts/ for per-user customization.
The Agent reads prompts from the user workspace, not from configs/prompts/.

Prompt files:
    SOUL.md            - Agent 身份、人格、能力、工作原则（含画像/技能指令）
    USER.md            - 用户画像（Agent 直接读写，无模板变量）
    SKILL_MATCH.md     - 用户输入匹配技能（LLM 兜底）
    SKILL_CONFIRM.md   - 候选技能确认提示

Usage:
    # Per-user prompt manager (in agent.py)
    from configs.prompts import PromptManager
    user_prompts = PromptManager(prompts_dir=workspace.prompts_dir())
    body = user_prompts.read_fresh_body("SOUL")

    # Default/fallback (reads from configs/prompts/)
    from configs.prompts import prompts
    body = prompts.render("SKILL_MATCH", skills_list="...", user_input="...")
"""

import re
from pathlib import Path

import yaml

from utils.logger import logger

PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptTemplate:
    """A single prompt template parsed from a .md file."""

    def __init__(self, name: str, description: str, variables: list[str], body: str):
        self.name = name
        self.description = description
        self.variables = variables
        self.body = body

    def render(self, **kwargs) -> str:
        """Render the template with the given variables.

        Uses a defaulting dict so missing variables stay as {key}
        instead of raising KeyError.
        """
        return self.body.format_map(_DefaultDict(kwargs))

    def __str__(self) -> str:
        return self.body


class _DefaultDict(dict):
    """Dict that returns '{key}' for missing keys instead of raising KeyError."""

    def __missing__(self, key):
        return "{" + key + "}"


class PromptManager:
    """Loads and caches all prompt templates from configs/prompts/*.md."""

    def __init__(self, prompts_dir: Path = PROMPTS_DIR):
        self._dir = prompts_dir
        self._cache: dict[str, PromptTemplate] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self._load_all()
            self._loaded = True

    def _load_all(self):
        if not self._dir.exists():
            logger.warning(f"Prompts directory not found: {self._dir}")
            return

        for md_file in sorted(self._dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
                template = self._parse_md(content, md_file.stem)
                self._cache[template.name] = template
                logger.debug(f"Loaded prompt: {template.name}")
            except Exception as e:
                logger.error(f"Failed to load prompt from {md_file}: {e}")

        logger.info(f"Loaded {len(self._cache)} prompt templates")

    @staticmethod
    def _parse_md(content: str, fallback_name: str) -> PromptTemplate:
        """Parse a .md file with YAML frontmatter + markdown body."""
        pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
        match = re.match(pattern, content, re.DOTALL)

        if match:
            yaml_str, body = match.groups()
            meta = yaml.safe_load(yaml_str) or {}
        else:
            meta = {}
            body = content

        return PromptTemplate(
            name=meta.get("name", fallback_name),
            description=meta.get("description", ""),
            variables=meta.get("variables", []),
            body=body.strip(),
        )

    def get(self, name: str) -> PromptTemplate | None:
        """Get a prompt template by name (from cache)."""
        self._ensure_loaded()
        return self._cache.get(name)

    def get_body(self, name: str) -> str:
        """Get the raw body text of a prompt (from cache)."""
        template = self.get(name)
        return template.body if template else ""

    def read_fresh(self, name: str) -> PromptTemplate | None:
        """Read a prompt directly from disk, bypassing cache.

        Use this for prompts that need to reflect file changes immediately
        (e.g., system prompts during development).
        """
        md_file = self._dir / f"{name}.md"
        if not md_file.exists():
            logger.warning(f"Prompt file not found: {md_file}")
            return None
        try:
            content = md_file.read_text(encoding="utf-8")
            return self._parse_md(content, name)
        except Exception as e:
            logger.error(f"Failed to read prompt '{name}': {e}")
            return None

    def read_fresh_body(self, name: str) -> str:
        """Read the raw body text directly from disk, bypassing cache."""
        template = self.read_fresh(name)
        return template.body if template else ""

    def render_fresh(self, name: str, **kwargs) -> str:
        """Read from disk and render with variables, bypassing cache."""
        template = self.read_fresh(name)
        if not template:
            logger.warning(f"Prompt '{name}' not found on disk")
            return ""
        return template.render(**kwargs)

    def render(self, name: str, **kwargs) -> str:
        """Render a prompt template with variables filled in (from cache)."""
        template = self.get(name)
        if not template:
            logger.warning(f"Prompt '{name}' not found")
            return ""
        return template.render(**kwargs)

    def list_all(self) -> list[dict]:
        """List all available prompts with metadata."""
        self._ensure_loaded()
        return [
            {"name": t.name, "description": t.description, "variables": t.variables}
            for t in self._cache.values()
        ]

    def reload(self):
        """Force reload all prompts from disk (useful for hot-reload)."""
        self._cache.clear()
        self._loaded = False
        self._ensure_loaded()


# Global singleton
prompts = PromptManager()
