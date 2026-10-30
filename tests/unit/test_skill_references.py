"""Multi-file skill proposals, persistence and lazy reference loading."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import skills as router_module
from core.plugins.internal import SkillParams, SkillTool, SubAgentParams, SubAgentTool
from core.skills import Skill, skill_from_md
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
        "function parseSkillProposal"
        + html.split("function parseSkillProposal", 1)[1].split("// ========== Scheduled Tasks", 1)[0]
    )
    result = subprocess.run(
        ["node", "-e", functions + "\n" + script],
        input=json.dumps(input_data, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_frontend_preserves_multiline_references_and_old_proposals():
    proposal = {
        "name": "buyer-leads",
        "description": "Screen buyer leads",
        "triggers": ["find buyers"],
        "instructions": "1. Confirm inputs.\n2. Read detailed rules.",
        "references": {
            "rules.md": "# Rules\n\nUnicode explanation\n```python\nprint('ok')\n```",
            "examples/report.md": "# Example\n\n" + "Complete contents\n" * 1500,
        },
    }
    result = run_frontend(
        """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const parsed = parseSkillProposal(JSON.stringify(input));
if(skillProposalVisibleText('Suggested workflow:[SKILL_PROPOSAL]'+JSON.stringify(input)+'[/SKILL_PROPOSAL]')!=='Suggested workflow:') throw new Error('raw proposal exposed');
if(skillProposalVisibleText('[SKILL_PROPOSAL]{')!=='') throw new Error('partial proposal exposed');
const legacy = parseSkillProposal('name: old-skill\\ndescription: old\\ntriggers: one, two\\ninstructions: |\\n  1. Run');
process.stdout.write(JSON.stringify({parsed, legacy}));
""",
        proposal,
    )
    assert result["parsed"]["references"] == proposal["references"]
    assert result["parsed"]["instructions"] == proposal["instructions"]
    assert result["legacy"]["references"] == {}
    assert "1. Run" in result["legacy"]["instructions"]


def test_frontend_rejects_reference_format_instead_of_silently_dropping_it():
    result = run_frontend(
        """
const errors = [];
for (const text of [
  'name: old\\ndescription: old\\ninstructions: |\\n  run\\nreferences:\\n  rules.md: body',
  JSON.stringify({name:'new',description:'new',instructions:'run',references:['bad']}),
  JSON.stringify({name:'new',description:'new',instructions:'run',references:{'rules.md':''}})
]) {
  try { parseSkillProposal(text); errors.push(false); } catch { errors.push(true); }
}
process.stdout.write(JSON.stringify(errors));
""",
        {},
    )
    assert result == [True, True, True]


@pytest.mark.asyncio
async def test_confirmation_payload_saves_main_and_references(workspace, monkeypatch):
    proposal = {
        "name": "buyer-leads",
        "description": "Screen buyer leads",
        "triggers": ["find buyers"],
        "instructions": "1. Confirm inputs.\n2. Screen using detailed rules.",
        "references": {"rules.md": "# Rules\n\nDetailed rules", "examples/report.md": "Report template"},
    }
    payload = run_frontend(
        """
const input = JSON.parse(require('fs').readFileSync(0,'utf8'));
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.dataset={}; this.style={}; this.handlers={}; }
  appendChild(child) { child.parentElement=this; this.children.push(child); return child; }
  append(...children) { children.forEach(child=>this.appendChild(child)); }
  addEventListener(type, handler) { this.handlers[type]=handler; }
  querySelectorAll(selector) { return this.children.flatMap(child=>[
    ...(child.className===selector.slice(1)?[child]:[]), ...child.querySelectorAll(selector)]); }
  closest() { return this.parentElement; }
  remove() {}
}
global.document = {createElement: tag=>new Element(tag), getElementById:()=>({value:'alice'})};
global.showSkillToast=()=>{}; global.loadSidebarSkills=()=>{};
let payload;
global.fetch=async (_url, options)=>{ payload=JSON.parse(options.body); return {ok:true}; };
const parent = new Element('div'), bubble = new Element('div'); parent.appendChild(bubble);
const text='[SKILL_PROPOSAL]'+JSON.stringify(input)+'[/SKILL_PROPOSAL]';
appendSkillProposals(bubble,text,'session-origin');
appendSkillProposals(bubble,text,'session-origin');
appendSkillProposals(bubble,'[SKILL_PROPOSAL]{','session-origin');
if(!parent.children.some(node=>(node.textContent||'').includes('output is incomplete'))) throw new Error('missing truncation notice');
const cards=parent.querySelectorAll('.skill-proposal-card');
if(cards.length!==1) throw new Error('duplicate proposal cards');
if(cards[0].children.filter(child=>child.tag==='details').length!==3) throw new Error('missing file previews');
(async()=>{
  await cards[0].children.find(child=>child.className==='skill-confirm-btn').handlers.click();
  process.stdout.write(JSON.stringify(payload));
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
""",
        proposal,
    )
    monkeypatch.setattr(router_module, "_get_manager", lambda: SkillManager(workspace=workspace))
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/skills")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/skills/alice/create", json=payload)
    assert response.status_code == 200
    loaded = SkillManager(workspace=workspace).get_all_skills("alice")["buyer-leads"]
    assert loaded.references == proposal["references"]
    assert loaded.source == "extracted"
    assert loaded.extracted_from == "session-origin"
    assert "references/rules.md" in loaded.instructions
    assert "references/examples/report.md" in loaded.instructions
    root = workspace._backend.root / "alice" / "skills" / "buyer-leads"
    assert (root / "references" / "rules.md").read_text() == proposal["references"]["rules.md"]
    assert proposal["references"]["rules.md"] not in workspace.skill_get("buyer-leads")


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
async def test_tool_save_and_get_ref_accept_canonical_and_prefixed_paths(workspace):
    manager = SkillManager(workspace=workspace)
    tool = SkillTool(user_id="alice", skill_manager=manager, workspace=workspace, session_id="origin")
    result = await tool(
        SkillParams(
            action="save",
            name="demo",
            description="demo",
            instructions="Steps",
            source="extracted",
            references={"references/rules.md": "Long detailed rules"},
        )
    )
    assert "saved" in result
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


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_sub_agent_proposal_bypasses_summary_truncation(workspace, monkeypatch, complete):
    import core.sub_agent as sub_module

    body = "[SKILL_PROPOSAL]" + json.dumps(
        {
            "name": "long-skill",
            "description": "long",
            "instructions": "Read the rules",
            "references": {"rules.md": "Complete body" * 3000},
        },
        ensure_ascii=False,
    )
    if complete:
        body += "[/SKILL_PROPOSAL]"

    class Reviewer:
        def __init__(self, **kwargs):
            pass

        async def run_stream(self, *args):
            yield {"code": 2, "content": body[:4000]}
            yield {"code": 2, "content": body[4000:]}

    monkeypatch.setattr(sub_module, "SubAgentRunner", Reviewer)
    tool = SubAgentTool(user_id="alice", workspace=workspace)
    summary = await tool(SubAgentParams(agent_type="session-reviewer", task="Extract skill"))
    assert tool._sub_events == [{"code": 12, "content": body}]
    assert len(summary) < 4000
    assert "Complete body" not in summary
    assert ("confirmation card" if complete else "output is incomplete") in summary


@pytest.mark.asyncio
async def test_stream_forwards_full_proposal_event_before_tool_summary(workspace):
    from agents import Agent
    from agents.items import ToolCallItem, ToolCallOutputItem
    from agents.stream_events import RunItemStreamEvent
    from openai.types.responses import ResponseFunctionToolCall

    from core.agent import _stream_sdk_events

    proposal = "[SKILL_PROPOSAL]" + "Complete reference body" * 3000 + "[/SKILL_PROPOSAL]"
    tool = SubAgentTool(user_id="alice", workspace=workspace)
    tool._sub_events.append({"code": 12, "content": proposal})
    agent = Agent(name="offline")

    class Result:
        async def stream_events(self):
            yield RunItemStreamEvent(
                name="tool_called",
                item=ToolCallItem(
                    agent=agent,
                    raw_item=ResponseFunctionToolCall(
                        type="function_call",
                        call_id="review",
                        name="sub_agent",
                        arguments="{}",
                    ),
                ),
            )
            yield RunItemStreamEvent(
                name="tool_output",
                item=ToolCallOutputItem(
                    agent=agent,
                    raw_item={"type": "function_call_output", "call_id": "review", "output": "summary"},
                    output="summary",
                ),
            )

    events = [event async for event in _stream_sdk_events(Result(), [tool])]
    assert [event["code"] for event in events] == [4, 12, 5]
    assert events[1]["content"] == proposal
