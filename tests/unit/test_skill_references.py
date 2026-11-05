"""Multi-file skill proposals: the confirmation card, persistence and lazy reference loading."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import skills as router_module
from core.plugins.internal import SkillParams, SkillTool
from core.skills import Skill, proposals, skill_from_md
from core.skills.manager import SkillManager
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", backend=LocalFSBackend(root=tmp_path))


def run_frontend(script, input_data):
    if not shutil.which("node"):
        pytest.skip("Node.js required for frontend proposal checks")
    html = Path("app/static/index.html").read_text()
    functions = (
        "function skillProposalUser"
        + html.split("function skillProposalUser", 1)[1].split("// ========== Scheduled Tasks", 1)[0]
    )
    result = subprocess.run(
        ["node", "-e", functions + "\n" + script],
        input=json.dumps(input_data, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


FAKE_DOM = """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.dataset={}; this.style={}; this.handlers={}; this.innerHTML=''; }
  appendChild(child) { child.parentElement=this; this.children.push(child); return child; }
  append(...children) { children.forEach(child=>this.appendChild(child)); }
  addEventListener(type, handler) { this.handlers[type]=handler; }
  querySelectorAll(selector) { return this.children.flatMap(child=>[
    ...(child.className===selector.slice(1)?[child]:[]), ...child.querySelectorAll(selector)]); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c=>c!==this); }
}
global.document = {createElement: tag=>new Element(tag), getElementById:()=>({value:'alice'})};
global.showSkillToast=()=>{}; global.loadSidebarSkills=()=>{};
const calls=[];
global.fetch=async (url, options)=>{ calls.push({url, method:options.method, body:options.body}); return {ok:true, json:async()=>({})}; };
const parent = new Element('div'), bubble = new Element('div'); parent.appendChild(bubble);
const button = (card, cls) => card.children.find(child=>child.className===cls);
"""


def create_event(**overrides):
    event = {
        "type": "skill_proposal",
        "proposal_id": "a" * 32,
        "operation": "create",
        "name": "buyer-leads",
        "fields": {
            "description": "Screen buyer leads",
            "triggers": ["find buyers"],
            "required_tools": [],
            "tags": [],
            "instructions": "1. Confirm inputs.\n2. Read detailed rules.",
        },
        "references": {
            "rules.md": "# Rules\n\nUnicode explanation\n```python\nprint('ok')\n```",
            "examples/report.md": "# Example\n\n" + "Complete contents\n" * 1500,
        },
    }
    return event | overrides


def test_card_shows_every_file_and_decides_by_proposal_id_only():
    result = run_frontend(
        FAKE_DOM
        + """
appendSkillProposal(bubble, JSON.stringify(input));
appendSkillProposal(bubble, JSON.stringify(input));
appendSkillProposal(bubble, '{not json');
const cards = parent.querySelectorAll('.skill-proposal-card');
const details = cards[0].children.filter(child=>child.tag==='details');
const titles = details.map(d=>d.children[0].textContent);
const notice = parent.children.some(node=>(node.textContent||'').includes('could not be displayed'));
(async()=>{
  await button(cards[0], 'skill-confirm-btn').handlers.click();
  await button(cards[0], 'skill-skip-btn').handlers.click();
  process.stdout.write(JSON.stringify({cards: cards.length, titles, notice, calls}));
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
""",
        create_event(),
    )
    assert result["cards"] == 1 and result["notice"]
    assert [t.split("(")[0] for t in result["titles"]] == [
        "SKILL.md",
        "references/rules.md",
        "references/examples/report.md",
    ]
    confirm, reject = result["calls"]
    assert (
        confirm["url"] == "/api/skills/alice/proposals/" + "a" * 32 + "/confirm"
        and confirm["method"] == "POST"
    )
    assert "body" not in confirm  # the request carries nothing; the server applies its staged copy
    assert reject["url"] == "/api/skills/alice/proposals/" + "a" * 32 and reject["method"] == "DELETE"


def test_patch_card_shows_the_fragment_being_replaced():
    result = run_frontend(
        FAKE_DOM
        + """
appendSkillProposal(bubble, JSON.stringify(input));
const card = parent.querySelectorAll('.skill-proposal-card')[0];
const titles = card.children.filter(child=>child.tag==='details').map(d=>d.children[0].textContent.split('(')[0]);
const bodies = card.children.filter(child=>child.tag==='details').map(d=>d.children[1].textContent);
process.stdout.write(JSON.stringify({heading: card.children[0].textContent, titles, bodies}));
""",
        {
            "type": "skill_proposal",
            "proposal_id": "b" * 32,
            "operation": "patch",
            "name": "demo",
            "ref_filename": "rules.md",
            "old_string": "old text",
            "new_string": "",
        },
    )
    assert result["heading"] == "Edit: demo (references/rules.md)"
    assert result["titles"] == ["Replace", "With"]
    assert result["bodies"] == ["old text", "(delete this text)"]


@pytest.mark.asyncio
async def test_confirming_a_proposal_saves_main_and_references(workspace, monkeypatch):
    manager = SkillManager(workspace=workspace)
    tool = SkillTool(user_id="alice", skill_manager=manager, workspace=workspace, session_id="session-origin")
    refs = {"rules.md": "# Rules\n\nDetailed rules", "examples/report.md": "Report template"}
    result = await tool(
        SkillParams(
            action="propose",
            operation="create",
            name="buyer-leads",
            description="Screen buyer leads",
            triggers=["find buyers"],
            instructions="1. Confirm inputs.\n2. Screen using detailed rules.",
            references=refs,
        )
    )
    proposal_id = json.loads(result)["proposal_id"]
    monkeypatch.setattr(router_module, "_get_manager", lambda: manager)
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/skills")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/skills/alice/proposals/{proposal_id}/confirm")
    assert response.status_code == 200
    loaded = manager.get_all_skills("alice")["buyer-leads"]
    assert loaded.references == refs
    assert loaded.source == "extracted" and loaded.extracted_from == "session-origin"
    assert "references/rules.md" in loaded.instructions
    assert "references/examples/report.md" in loaded.instructions
    root = workspace._backend.root / "alice" / "skills" / "buyer-leads"
    assert (root / "references" / "rules.md").read_text() == refs["rules.md"]
    assert refs["rules.md"] not in workspace.skill_get("buyer-leads")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "references",
    [
        {"../escape.md": "bad"},
        {"/escape.md": "bad"},
        {"references/../escape.md": "bad"},
        {"C:\\escape.md": "bad"},
        {"rules.md": "one", "references/rules.md": "two"},
        {"empty.md": ""},
    ],
)
async def test_invalid_references_do_not_publish_skill(workspace, monkeypatch, references):
    monkeypatch.setattr(router_module, "_get_manager", lambda: SkillManager(workspace=workspace))
    app = FastAPI()
    app.include_router(router_module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/alice/create",
            json={
                "name": "invalid-skill",
                "description": "invalid",
                "triggers": [],
                "instructions": "steps",
                "references": references,
            },
        )
    assert response.status_code == 422
    assert workspace.skill_get("invalid-skill") is None
    assert workspace.skill_refs_get("invalid-skill") == {}


@pytest.mark.asyncio
async def test_get_ref_accepts_canonical_and_prefixed_paths_after_confirmation(workspace):
    manager = SkillManager(workspace=workspace)
    tool = SkillTool(user_id="alice", skill_manager=manager, workspace=workspace, session_id="origin")
    result = await tool(
        SkillParams(
            action="propose",
            name="demo",
            description="demo",
            instructions="Steps",
            references={"references/rules.md": "Long detailed rules"},
        )
    )
    proposals.commit(manager, "alice", json.loads(result)["proposal_id"])
    assert workspace.skill_refs_get("demo") == {"rules.md": "Long detailed rules"}
    assert "Long detailed rules" in await tool(
        SkillParams(action="get_ref", name="demo", ref_filename="rules.md")
    )
    assert "Long detailed rules" in await tool(
        SkillParams(action="get_ref", name="demo", ref_filename="references/rules.md")
    )
    assert "references/rules.md" in workspace.skill_get("demo")
    assert "Long detailed rules" not in workspace.skill_get("demo")


def test_updates_merge_references_and_keep_reference_paths_unchanged(workspace):
    manager = SkillManager(workspace=workspace)
    manager.save_user_skill(
        "alice",
        Skill(
            name="demo",
            description="demo",
            instructions="Run the workflow",
            references={"old.md": "old"},
        ),
    )
    manager.update_user_skill("alice", "demo", {"references": {"update_memory.md": "new details"}})
    loaded = manager.get_all_skills("alice")["demo"]
    assert loaded.references == {"old.md": "old", "update_memory.md": "new details"}
    assert "references/update_memory.md" in loaded.instructions
    raw = skill_from_md(
        "---\nname: legacy\ndescription: legacy\n---\n"
        'Run scripts/update_memory.py, then call update_memory(action="read").'
    )
    assert "scripts/update_memory.py" in raw.instructions
    assert 'memory(action="read")' in raw.instructions
