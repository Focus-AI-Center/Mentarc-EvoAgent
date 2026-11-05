"""Intentional skill updates preserve lifecycle and confirmed proposal intent."""

import json
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import skills as router
from core.plugins.internal import SkillParams, SkillTool
from core.skills import Skill, proposals
from core.skills.manager import SkillManager
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture
def manager(tmp_path):
    return SkillManager(Workspace("alice", LocalFSBackend(tmp_path)))


def original(name="demo"):
    return Skill(
        name=name,
        description="Old description",
        instructions="Old steps",
        version=7,
        usage_count=12,
        source="extracted",
        extracted_from="origin",
        tags=["writing"],
        created_at=datetime(2025, 1, 1),
        required_tools=["fetch_url"],
        references={"old.md": "Keep old details"},
        requires_sub_agent=True,
        sub_agent_name="worker",
    )


@pytest.mark.asyncio
async def test_confirmed_update_preserves_lifecycle_dependencies_and_handoff(manager):
    manager.save_user_skill("alice", original())
    tool = SkillTool("alice", manager, workspace=manager._workspace, session_id="new-session")
    result = await tool(
        SkillParams(
            action="propose",
            operation="update",
            name="demo",
            description="New description",
            instructions="New steps",
        )
    )
    proposals.commit(manager, "alice", json.loads(result)["proposal_id"])
    saved = manager.get_all_skills("alice")["demo"]
    assert saved.version == 8 and saved.usage_count == 12
    assert saved.created_at == datetime(2025, 1, 1) and saved.extracted_from == "origin"
    assert saved.source == "extracted" and saved.tags == ["writing"]
    assert saved.required_tools == ["fetch_url"]
    assert saved.requires_sub_agent and saved.sub_agent_name == "worker"
    assert saved.references == {"old.md": "Keep old details"}
    assert saved.description == "New description" and "New steps" in saved.instructions


@pytest.mark.asyncio
async def test_create_rejects_duplicates_and_nested_update_preserves_metadata(manager, monkeypatch):
    manager.save_user_skill("alice", original("parent/child"))
    monkeypatch.setattr(router, "_get_manager", lambda: manager)
    app = FastAPI()
    app.include_router(router.router)
    payload = {"name": "parent/child", "description": "duplicate", "instructions": "bad", "triggers": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/alice/create", json=payload)
        assert response.status_code == 409
        response = await client.put(
            "/alice/parent/child",
            json={"instructions": "Updated steps", "references": {"new.md": "New details"}, "triggers": []},
        )
    assert response.status_code == 200
    saved = manager.get_all_skills("alice")["parent/child"]
    assert saved.version == 8 and saved.usage_count == 12
    assert saved.source == "extracted" and saved.extracted_from == "origin"
    assert saved.references == {"old.md": "Keep old details", "new.md": "New details"}


@pytest.mark.asyncio
async def test_update_proposal_preserves_explicit_empty_lists_and_omitted_fields(manager):
    manager.save_user_skill("alice", original())
    tool = SkillTool("alice", manager, workspace=manager._workspace, session_id="new-session")
    result = await tool(
        SkillParams(action="propose", operation="update", name="demo", description="Updated", triggers=[])
    )
    event = proposals.proposal_event(manager._workspace.proposal_get(json.loads(result)["proposal_id"]))
    assert event["fields"] == {"description": "Updated", "triggers": []}
    proposals.commit(manager, "alice", event["proposal_id"])
    saved = manager.get_all_skills("alice")["demo"]
    assert saved.triggers == []
    assert saved.required_tools == ["fetch_url"] and saved.tags == ["writing"]


@pytest.mark.asyncio
async def test_create_and_update_preserve_declared_parameter_models(manager, monkeypatch):
    monkeypatch.setattr(router, "_get_manager", lambda: manager)
    app = FastAPI()
    app.include_router(router.router)
    payload = {
        "name": "parameterized",
        "description": "Report",
        "instructions": "Write a report",
        "triggers": [],
        "parameters": [{"name": "topic", "description": "Report topic", "example": "Energy"}],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/alice/create", json=payload)
        assert response.status_code == 200
        response = await client.put("/alice/parameterized", json={"description": "Updated report"})
    assert response.status_code == 200
    saved = manager.get_all_skills("alice")["parameterized"]
    assert saved.parameters[0].name == "topic" and saved.parameters[0].example == "Energy"
