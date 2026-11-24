"""Embedding pre-matching must not block concurrent agent and worker execution."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.skills.matcher as module
from core.skills import Skill
from utils.usage import current_usage, usage_scope


def matcher(monkeypatch):
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kwargs: None)
    return module.SkillMatcher(SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("block_input", [True, False])
async def test_embedding_wait_keeps_the_event_loop_available(monkeypatch, block_input):
    started, release = threading.Event(), threading.Event()
    collectors = []

    def embed(text):
        collectors.append(current_usage())
        if (text == "input") is block_input:
            started.set()
            release.wait(2)
        return [1.0, 0.0]

    monkeypatch.setattr(module, "get_embedding", embed)
    engine = matcher(monkeypatch)
    skills = {"research": Skill(name="research", description="Research supplied sources")}
    with usage_scope() as collector:
        task = asyncio.create_task(engine.match("input", "alice", skills))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert not task.done(), "The embedding wait blocked other event-loop work"
            release.set()
            result = await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert result.skill.name == "research" and result.match_type == "embedding"
    assert collectors and all(item is collector for item in collectors)


@pytest.mark.asyncio
async def test_unmatched_input_returns_to_the_main_agent_without_a_separate_matching_call(monkeypatch):
    monkeypatch.setattr(module, "get_embedding", lambda text: [])
    engine = matcher(monkeypatch)
    engine._llm_match = AsyncMock(side_effect=AssertionError("No dedicated matching-model call"))
    result = await engine.match(
        "unmatched input", "alice", {"research": Skill(name="research", description="Research")}
    )
    assert result.skill is None
    engine._llm_match.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyword_match_does_not_issue_embedding_requests(monkeypatch):
    def unexpected(text):
        raise AssertionError("Keyword matches do not need embeddings")

    monkeypatch.setattr(module, "get_embedding", unexpected)
    engine = matcher(monkeypatch)
    skill = Skill(name="research", description="Research", triggers=["compare sources"])
    result = await engine.match("COMPARE SOURCES for this report", "alice", {"research": skill})
    assert result.skill is skill and result.match_type == "keyword"
