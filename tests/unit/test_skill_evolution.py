"""Intentional skill updates preserve lifecycle and confirmed proposal intent."""

from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import skills as router
from core.plugins.internal import SkillParams, SkillTool
from core.skills import Skill
from core.skills.manager import SkillManager
from tests.unit.test_skill_references import run_frontend
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
async def test_same_name_save_preserves_lifecycle_dependencies_and_handoff(manager):
    manager.save_user_skill("alice", original())
    tool = SkillTool("alice", manager, workspace=manager._workspace, session_id="new-session")
    await tool(
        SkillParams(action="save", name="demo", description="New description", instructions="New steps")
    )
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


def test_confirmed_update_proposal_uses_the_update_endpoint():
    result = run_frontend(
        """
const proposal = parseSkillProposal(JSON.stringify({name:'parent/child', update_target:'parent/child',
  description:'Updated', instructions:'New steps', references:{'rules.md':'New rules'}}));
global.document={getElementById:()=>({value:'alice'})};
global.showSkillToast=()=>{}; global.loadSidebarSkills=()=>{};
let captured;
global.fetch=async (url,options)=>{captured={url,method:options.method,body:JSON.parse(options.body)};return {ok:true};};
const button={closest:()=>({innerHTML:''})};
(async()=>{await confirmSkillProposal(button,btoa(unescape(encodeURIComponent(JSON.stringify(proposal)))));
process.stdout.write(JSON.stringify({proposal,captured}));})().catch(e=>{process.stderr.write(String(e));process.exitCode=1;});
""",
        {},
    )
    assert result["proposal"]["operation"] == "update"
    assert result["captured"]["method"] == "PUT"
    assert result["captured"]["url"] == "/api/skills/alice/parent%2Fchild"
    assert result["captured"]["body"]["references"] == {"rules.md": "New rules"}
    assert "source" not in result["captured"]["body"]
    assert not {"triggers", "required_tools", "tags"} & result["captured"]["body"].keys()


def test_update_proposal_preserves_explicit_empty_lists():
    result = run_frontend(
        """
const proposal=parseSkillProposal(JSON.stringify({name:'demo',operation:'update',description:'Updated',
instructions:'New steps',triggers:[],required_tools:[],tags:[]}));
process.stdout.write(JSON.stringify(proposal));
""",
        {},
    )
    assert result["triggers"] == result["required_tools"] == result["tags"] == []


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
