"""Skill matcher - three-stage matching: keyword → embedding → LLM."""

import httpx
from openai import AsyncOpenAI

from configs.prompts import prompts
from configs.settings import (
    COMPRESSION_MODEL,
    OPENAI_API_BASE_SECONDARY,
    OPENAI_API_KEY,
    PROXY_SERVER,
    SKILL_MATCH_EMBEDDING_THRESHOLD,
)
from core.skills import Skill, SkillMatchResult
from core.skills.manager import SkillManager
from utils.embedding import cosine_similarity, get_embedding
from utils.logger import logger


class SkillMatcher:
    """Three-stage skill matching: keyword → embedding → LLM."""

    def __init__(self, skill_manager: SkillManager):
        self.skill_manager = skill_manager
        kwargs = dict(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE_SECONDARY)
        if PROXY_SERVER:
            kwargs["http_client"] = httpx.AsyncClient(proxy=PROXY_SERVER)
        self._llm_client = AsyncOpenAI(**kwargs)
        # Cache: skill_name -> embedding
        self._skill_embeddings: dict[str, list[float]] = {}

    async def match(
        self,
        user_input: str,
        user_id: str,
        skills: dict[str, Skill] | None = None,
    ) -> SkillMatchResult:
        """Match user input against available skills using three stages.

        Stage 1: Keyword quick match (< 1ms)
        Stage 2: Embedding similarity (< 50ms)
        Stage 3: LLM intent matching (< 500ms, fallback)

        If *skills* is provided, uses that dict directly instead of
        reading from disk (avoids redundant I/O when the caller already
        loaded the skills catalog).
        """
        if skills is None:
            skills = self.skill_manager.get_all_skills(user_id)
        if not skills:
            return SkillMatchResult()

        # Stage 1: Keyword match
        result = self._keyword_match(user_input, skills)
        if result.skill:
            return result

        # Stage 2: Embedding match
        result = await self._embedding_match(user_input, skills)
        if result.skill:
            return result

        # No match — let master agent handle intent naturally
        return SkillMatchResult()

    def _keyword_match(self, user_input: str, skills: dict[str, Skill]) -> SkillMatchResult:
        """Stage 1: Fast keyword-based trigger matching."""
        user_input_lower = user_input.lower()
        for skill in skills.values():
            for trigger in skill.triggers:
                if trigger.lower() in user_input_lower:
                    return SkillMatchResult(
                        skill=skill,
                        match_type="keyword",
                        confidence=1.0,
                    )
        return SkillMatchResult()

    async def _embedding_match(self, user_input: str, skills: dict[str, Skill]) -> SkillMatchResult:
        """Stage 2: Embedding-based semantic similarity matching."""
        try:
            input_embedding = get_embedding(user_input)
        except Exception as e:
            logger.warning(f"Embedding match failed (input): {e}")
            return SkillMatchResult()

        best_skill = None
        best_score = 0.0

        for _name, skill in skills.items():
            skill_embedding = await self._get_skill_embedding(skill)
            if not skill_embedding:
                continue

            score = cosine_similarity(input_embedding, skill_embedding)
            if score > best_score:
                best_score = score
                best_skill = skill

        if best_skill and best_score >= SKILL_MATCH_EMBEDDING_THRESHOLD:
            return SkillMatchResult(
                skill=best_skill,
                match_type="embedding",
                confidence=best_score,
            )
        return SkillMatchResult()

    async def _get_skill_embedding(self, skill: Skill) -> list[float] | None:
        """Get or compute embedding for a skill's description."""
        if skill.name in self._skill_embeddings:
            return self._skill_embeddings[skill.name]

        triggers = ", ".join(skill.triggers) if skill.triggers else ""
        text = f"{skill.name}: {skill.description} ({triggers})"
        try:
            embedding = get_embedding(text)
            self._skill_embeddings[skill.name] = embedding
            return embedding
        except Exception as e:
            logger.warning(f"Failed to embed skill '{skill.name}': {e}")
            return None

    async def _llm_match(self, user_input: str, skills: dict[str, Skill]) -> SkillMatchResult:
        """Stage 3: LLM-based intent matching (fallback)."""
        skills_desc = "\n".join(
            f"- **{s.name}**: {s.description} (Triggers: {', '.join(s.triggers)})" for s in skills.values()
        )

        prompt = prompts.render(
            "SKILL_MATCH",
            user_input=user_input,
            skills_description=skills_desc,
        )

        try:
            response = await self._llm_client.chat.completions.create(
                model=COMPRESSION_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Return only the exact skill name or null, with no other content.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_completion_tokens=100,
            )
            answer = response.choices[0].message.content.strip().strip('"')

            if answer and answer != "null" and answer in skills:
                return SkillMatchResult(
                    skill=skills[answer],
                    match_type="llm",
                    confidence=0.7,
                )
        except Exception as e:
            logger.error(f"LLM skill matching failed: {e}")

        return SkillMatchResult()

    def invalidate_cache(self, skill_name: str = None):
        """Invalidate skill embedding cache."""
        if skill_name:
            self._skill_embeddings.pop(skill_name, None)
        else:
            self._skill_embeddings.clear()
