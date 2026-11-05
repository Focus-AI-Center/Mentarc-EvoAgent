"""Agents propose skill changes; only a user's confirmation writes them."""

import json
import unicodedata
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.routers import skills as router_module
from core.plugins.internal import SkillParams, SkillTool
from core.skills import Skill, proposals
from core.skills.manager import SkillManager
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", backend=LocalFSBackend(root=tmp_path))


@pytest.fixture
def manager(workspace):
    return SkillManager(workspace)


@pytest.fixture
def tool(workspace, manager):
    return SkillTool(user_id="alice", skill_manager=manager, workspace=workspace, session_id="origin")


def existing_skill(manager, name="demo", **overrides):
    skill = Skill(
        name=name,
        description="Old description",
        instructions="1. Confirm inputs.\n2. Check evidence.\n3. Write the report.",
        triggers=["write report"],
        required_tools=["fetch_url"],
        tags=["writing"],
        references={"rules.md": "Keep source links."},
        source="extracted",
        extracted_from="earlier",
        version=7,
        usage_count=12,
        **overrides,
    )
    manager.save_user_skill("alice", skill)
    return skill


async def propose(tool, **fields):
    result = await tool(SkillParams(action="propose", **fields))
    return result, (json.loads(result) if result.startswith("{") else None)


# --- the agent cannot write -------------------------------------------------------------------


def test_tool_exposes_only_read_and_propose_actions(tool):
    from core.agent_compat import convert_tool

    schema = convert_tool(tool).params_json_schema
    assert set(schema["properties"]["action"]["enum"]) == {"list", "get", "get_ref", "propose"}
    for removed in ("save", "patch", "delete", "ref_set", "ref_del"):
        with pytest.raises(ValidationError):
            SkillParams(action=removed, name="demo")


@pytest.mark.asyncio
async def test_propose_stages_without_writing_and_confirmation_saves(tool, workspace, manager):
    result, data = await propose(
        tool,
        operation="create",
        name="demo",
        description="Screen buyer leads",
        instructions="1. Confirm inputs.\n2. Screen using the rules.",
        triggers=["find buyers"],
        references={"references/rules.md": "# Rules", "examples/report.md": "Template"},
    )
    assert (
        data["status"] == "pending_confirmation"
        and "not saved" in data["note"].lower()
        or "only after" in data["note"]
    )
    assert workspace.skill_get("demo") is None and workspace.skill_refs_get("demo") == {}
    assert [r["id"] for r in proposals.list_pending(manager, "alice")] == [data["proposal_id"]]

    record, skill = proposals.commit(manager, "alice", data["proposal_id"])
    saved = manager.get_all_skills("alice")["demo"]
    assert saved.source == "extracted" and saved.extracted_from == "origin" and saved.created_by == "alice"
    assert saved.references == {"rules.md": "# Rules", "examples/report.md": "Template"}
    assert saved.triggers == ["find buyers"]
    assert workspace.proposal_list() == []
    # The card showed exactly the text that was saved, links included.
    assert record["fields"]["instructions"] == saved.instructions
    assert (
        "references/rules.md" in saved.instructions and "references/examples/report.md" in saved.instructions
    )
    assert "# Rules" not in workspace.skill_get("demo")


@pytest.mark.asyncio
async def test_event_sink_gets_the_full_proposal_while_the_model_sees_a_receipt(tool):
    events = []

    async def sink(event):
        events.append(event)

    tool.event_sink = sink
    body = "Complete reference body\n" * 2500
    result, data = await propose(
        tool, name="demo", description="d", instructions="Steps", references={"rules.md": body}
    )
    assert [event["code"] for event in events] == [12]
    shown = json.loads(events[0]["content"])
    assert shown["type"] == "skill_proposal" and shown["proposal_id"] == data["proposal_id"]
    assert shown["references"]["rules.md"] == body
    assert body not in result and len(result) < 1000


@pytest.mark.asyncio
async def test_staged_proposals_are_not_skills(tool, workspace, manager):
    await propose(tool, name="demo", description="d", instructions="Steps", references={"a.md": "A"})
    assert "demo" not in workspace.skill_get_all()
    assert "demo" not in workspace.export_snapshot()["skills"]
    assert all("demo" not in key for key in workspace._backend.list_keys_sync("alice/skills/"))
    assert "demo" not in await tool(SkillParams(action="list"))
    workspace.clear_skills()
    assert len(workspace.proposal_list()) == 1


# --- validation at proposal time ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,message",
    [
        ({"references": {"big.md": "x" * (64 * 1024 + 1)}}, "exceeds 64KB"),
        ({"references": {f"f{i}.md": "x" for i in range(21)}}, "at most 20"),
        ({"references": {f"f{i}.md": "x" * 60_000 for i in range(5)}}, "256KB in total"),
        ({"instructions": "x" * (30 * 1024 + 1)}, "Instructions exceed 30KB"),
        ({"references": {"Rules.md": "a", "rules.md": "b"}}, "differ only by case"),
        (
            {
                "references": {
                    unicodedata.normalize("NFC", "é.md"): "a",
                    unicodedata.normalize("NFD", "é.md"): "b",
                }
            },
            "Duplicate reference path|differ only by case",
        ),
        ({"references": {"../escape.md": "bad"}}, "parent"),
        ({"references": {"/escape.md": "bad"}}, "relative"),
        ({"references": {"empty.md": ""}}, "nonempty"),
    ],
)
async def test_invalid_proposals_are_rejected_and_nothing_is_staged(tool, workspace, fields, message):
    base = {"name": "demo", "description": "d", "instructions": "Steps"} | fields
    result, _ = await propose(tool, **base)
    assert result.startswith("Proposal rejected:")
    import re

    assert re.search(message, result)
    assert workspace.proposal_list() == [] and workspace.skill_get("demo") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,message",
    [
        ({"operation": "create", "name": "demo", "description": "d", "instructions": "s"}, "already exists"),
        ({"operation": "update", "name": "missing", "description": "d"}, "does not exist"),
        ({"operation": "update", "name": "demo"}, "at least one field"),
        ({"operation": "create", "name": "new", "description": "d"}, "requires instructions"),
        (
            {"operation": "create", "name": "../escape", "description": "d", "instructions": "s"},
            "Invalid skill name",
        ),
        (
            {"operation": "create", "name": "", "description": "d", "instructions": "s"},
            "requires a skill name",
        ),
        ({"operation": "update", "name": "demo", "references": {"RULES.md": "x"}}, "collides with existing"),
    ],
)
async def test_proposal_preconditions(tool, workspace, manager, fields, message):
    existing_skill(manager)
    result, _ = await propose(tool, **fields)
    assert result.startswith("Proposal rejected:") and message in result
    assert workspace.proposal_list() == []


# --- update ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_changes_only_supplied_fields_and_keeps_references(tool, workspace, manager):
    existing_skill(manager)
    _, data = await propose(
        tool, operation="update", name="demo", description="New description", references={"extra.md": "Extra"}
    )
    proposals.commit(manager, "alice", data["proposal_id"])
    saved = manager.get_all_skills("alice")["demo"]
    assert saved.description == "New description"
    assert (
        saved.triggers == ["write report"]
        and saved.required_tools == ["fetch_url"]
        and saved.tags == ["writing"]
    )
    assert saved.references == {"rules.md": "Keep source links.", "extra.md": "Extra"}
    assert "references/extra.md" in saved.instructions
    assert saved.version == 8 and saved.usage_count == 12 and saved.extracted_from == "earlier"


@pytest.mark.asyncio
async def test_update_can_clear_a_list_only_when_it_is_explicitly_empty(tool, manager):
    existing_skill(manager)
    _, data = await propose(tool, operation="update", name="demo", description="Changed", triggers=[])
    proposals.commit(manager, "alice", data["proposal_id"])
    assert manager.get_all_skills("alice")["demo"].triggers == []
    existing = manager.get_all_skills("alice")["demo"]
    assert existing.tags == ["writing"] and existing.required_tools == ["fetch_url"]


@pytest.mark.asyncio
async def test_update_refused_when_the_skill_changed_after_it_was_proposed(tool, workspace, manager):
    existing_skill(manager)
    _, data = await propose(tool, operation="update", name="demo", instructions="Rewritten steps")
    manager.update_user_skill("alice", "demo", {"description": "Edited by the user meanwhile"})
    before = workspace.skill_get("demo")
    with pytest.raises(proposals.ProposalConflict):
        proposals.commit(manager, "alice", data["proposal_id"])
    assert workspace.skill_get("demo") == before
    assert workspace.proposal_get(data["proposal_id"]) is not None  # still there to skip


@pytest.mark.asyncio
async def test_usage_counting_does_not_make_an_update_stale(tool, manager):
    existing_skill(manager)
    _, data = await propose(tool, operation="update", name="demo", description="Changed")
    manager.increment_usage("alice", "demo")
    proposals.commit(manager, "alice", data["proposal_id"])
    assert manager.get_all_skills("alice")["demo"].description == "Changed"


@pytest.mark.asyncio
async def test_update_can_be_retried_after_a_partial_failure(tool, workspace, manager, monkeypatch):
    existing_skill(manager)
    _, data = await propose(
        tool, operation="update", name="demo", description="Changed", references={"extra.md": "Extra"}
    )
    real = workspace.skill_set

    def failing(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(workspace, "skill_set", failing)
    with pytest.raises(OSError):
        proposals.commit(manager, "alice", data["proposal_id"])
    assert workspace.skill_refs_get("demo")["extra.md"] == "Extra"  # references landed, main text did not
    monkeypatch.setattr(workspace, "skill_set", real)
    proposals.commit(manager, "alice", data["proposal_id"])
    saved = manager.get_all_skills("alice")["demo"]
    assert saved.description == "Changed" and "extra.md" in saved.references
    assert workspace.proposal_list() == []


@pytest.mark.asyncio
async def test_confirming_an_already_applied_proposal_is_harmless(tool, workspace, manager):
    existing_skill(manager)
    _, data = await propose(tool, operation="update", name="demo", description="Changed")
    record = workspace.proposal_get(data["proposal_id"])
    proposals.commit(manager, "alice", data["proposal_id"])
    version = manager.get_all_skills("alice")["demo"].version
    workspace.proposal_set(record["id"], record)  # the delete after a successful write was lost
    proposals.commit(manager, "alice", record["id"])
    assert manager.get_all_skills("alice")["demo"].version == version
    assert workspace.proposal_list() == []


@pytest.mark.asyncio
async def test_create_can_be_retried_after_a_partial_failure(tool, workspace, manager, monkeypatch):
    _, data = await propose(
        tool, name="demo", description="d", instructions="Steps", references={"rules.md": "Rules"}
    )
    real = workspace.skill_set
    monkeypatch.setattr(workspace, "skill_set", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        proposals.commit(manager, "alice", data["proposal_id"])
    assert workspace.skill_get("demo") is None
    monkeypatch.setattr(workspace, "skill_set", real)
    proposals.commit(manager, "alice", data["proposal_id"])
    assert manager.get_all_skills("alice")["demo"].references == {"rules.md": "Rules"}


# --- patch ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["", "rules.md", "references/rules.md"])
async def test_patch_is_staged_then_applied_with_the_rest_untouched(tool, workspace, manager, filename):
    existing_skill(manager)
    workspace.skill_resource_set("demo", "scripts/run.py", b"print('unchanged')\n")
    main_before, refs_before = workspace.skill_get("demo"), workspace.skill_refs_get("demo")
    old, new = (
        ("Keep source links.", "Keep source links and dates.")
        if filename
        else ("2. Check evidence.", "2. Check evidence twice.")
    )
    _, data = await propose(
        tool, operation="patch", name="demo", ref_filename=filename, old_string=old, new_string=new
    )
    assert workspace.skill_get("demo") == main_before and workspace.skill_refs_get("demo") == refs_before
    proposals.commit(manager, "alice", data["proposal_id"])
    if filename:
        assert workspace.skill_refs_get("demo")["rules.md"] == new
        assert workspace.skill_get("demo") == main_before
    else:
        assert workspace.skill_get("demo") == main_before.replace(old, new)
        assert workspace.skill_refs_get("demo") == refs_before
    assert workspace.skill_resources_get("demo")["scripts/run.py"] == b"print('unchanged')\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"old_string": "absent", "new_string": "x"}, "was not found"),
        ({"old_string": "Check", "new_string": "x"}, None),  # unique, valid
        ({"old_string": "", "new_string": "x"}, "needs a nonempty"),
        ({"old_string": "Check"}, "explicit new_string"),
        ({"new_string": "x"}, "needs a nonempty"),
        ({"old_string": "Check evidence.", "new_string": "Check evidence."}, "must differ"),
        ({"old_string": "description: Old description", "new_string": "x"}, "was not found"),
        ({"old_string": "Keep", "new_string": "x", "ref_filename": "missing.md"}, "does not exist"),
        ({"old_string": "Keep", "new_string": "x", "ref_filename": "../escape.md"}, "parent"),
    ],
)
async def test_patch_proposals_are_checked_up_front(tool, workspace, manager, fields, reason):
    existing_skill(manager)
    result, _ = await propose(tool, operation="patch", name="demo", **fields)
    if reason is None:
        assert result.startswith("{")
        return
    assert result.startswith("Proposal rejected:") and reason in result
    assert workspace.proposal_list() == []


@pytest.mark.asyncio
async def test_patch_that_no_longer_applies_is_refused_without_writing(tool, workspace, manager):
    existing_skill(manager)
    _, data = await propose(
        tool, operation="patch", name="demo", old_string="2. Check evidence.", new_string="2. Check twice."
    )
    manager.update_user_skill("alice", "demo", {"instructions": "Completely different steps"})
    before = workspace.skill_get("demo")
    with pytest.raises(proposals.ProposalConflict):
        proposals.commit(manager, "alice", data["proposal_id"])
    assert workspace.skill_get("demo") == before


@pytest.mark.asyncio
async def test_concurrent_write_during_patch_confirmation_is_not_overwritten(
    tool, workspace, manager, monkeypatch
):
    existing_skill(manager)
    _, data = await propose(
        tool, operation="patch", name="demo", old_string="2. Check evidence.", new_string="2. Check twice."
    )
    key = "alice/skills/demo/SKILL.md"
    backend = workspace._backend
    concurrent = backend.read_text_sync(key) + "\nA concurrent update.\n"
    write = backend.write_text_sync

    def race(target, content, *, if_match=None):
        assert target == key and if_match is not None
        write(key, concurrent)
        return write(target, content, if_match=if_match)

    monkeypatch.setattr(backend, "write_text_sync", race)
    with pytest.raises(proposals.ProposalConflict, match="changed while patching"):
        proposals.commit(manager, "alice", data["proposal_id"])
    assert backend.read_text_sync(key) == concurrent


# --- lifecycle --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_proposals_cannot_be_confirmed_and_are_purged(tool, workspace, manager):
    _, data = await propose(tool, name="demo", description="d", instructions="Steps")
    record = workspace.proposal_get(data["proposal_id"])
    record["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    workspace.proposal_set(record["id"], record)
    with pytest.raises(proposals.ProposalNotFound):
        proposals.commit(manager, "alice", record["id"])
    assert workspace.proposal_list() == [] and workspace.skill_get("demo") is None
    workspace.proposal_set(record["id"], record)
    assert proposals.list_pending(manager, "alice") == [] and workspace.proposal_list() == []


@pytest.mark.asyncio
async def test_pending_proposals_are_capped_per_user(tool, monkeypatch):
    monkeypatch.setattr(proposals, "MAX_PENDING_PER_USER", 2)
    for name in ("one", "two"):
        _, data = await propose(tool, name=name, description="d", instructions="Steps")
        assert data
    result, _ = await propose(tool, name="three", description="d", instructions="Steps")
    assert result.startswith("Proposal rejected:") and "waiting for confirmation" in result


@pytest.mark.asyncio
async def test_a_newer_create_replaces_the_older_but_patches_coexist(tool, workspace, manager):
    await propose(tool, name="demo", description="first", instructions="Steps")
    _, second = await propose(tool, name="demo", description="second", instructions="Steps")
    assert [r["id"] for r in workspace.proposal_list()] == [second["proposal_id"]]
    existing_skill(manager, "other")
    for old in ("1. Confirm inputs.", "3. Write the report."):
        await propose(tool, operation="patch", name="other", old_string=old, new_string=old + " Again.")
    assert len([r for r in workspace.proposal_list() if r["name"] == "other"]) == 2


@pytest.mark.asyncio
async def test_reject_discards_the_proposal(tool, workspace, manager):
    _, data = await propose(tool, name="demo", description="d", instructions="Steps")
    proposals.reject(manager, "alice", data["proposal_id"])
    assert workspace.proposal_list() == []
    with pytest.raises(proposals.ProposalNotFound):
        proposals.reject(manager, "alice", data["proposal_id"])
    with pytest.raises(proposals.ProposalNotFound):
        proposals.commit(manager, "alice", data["proposal_id"])


def test_proposal_ids_cannot_address_other_files(workspace):
    assert workspace.proposal_get("../../skills/demo/SKILL") is None
    assert workspace.proposal_del("../x") is False


# --- previous version and undo ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_restores_the_text_and_references_before_the_last_change(tool, workspace, manager):
    existing_skill(manager)
    before, refs = workspace.skill_get("demo"), workspace.skill_refs_get("demo")
    _, data = await propose(
        tool, operation="update", name="demo", instructions="Rewritten", references={"extra.md": "Extra"}
    )
    proposals.commit(manager, "alice", data["proposal_id"])
    changed = workspace.skill_get("demo")
    assert "extra.md" in workspace.skill_refs_get("demo")
    manager.undo_last_change("alice", "demo")
    assert workspace.skill_get("demo") == before and workspace.skill_refs_get("demo") == refs
    manager.undo_last_change("alice", "demo")  # an undo can itself be undone
    assert workspace.skill_get("demo") == changed and "extra.md" in workspace.skill_refs_get("demo")


@pytest.mark.asyncio
async def test_undo_after_a_patch_and_after_a_deletion(tool, workspace, manager):
    existing_skill(manager)
    before = workspace.skill_get("demo")
    _, data = await propose(
        tool, operation="patch", name="demo", old_string="2. Check evidence.", new_string="2. Check twice."
    )
    proposals.commit(manager, "alice", data["proposal_id"])
    manager.undo_last_change("alice", "demo")
    assert workspace.skill_get("demo") == before
    manager.delete_user_skill("alice", "demo")
    assert workspace.skill_get("demo") is None
    manager.undo_last_change("alice", "demo")
    assert workspace.skill_get("demo") == before and workspace.skill_refs_get("demo") == {
        "rules.md": "Keep source links."
    }


@pytest.mark.asyncio
async def test_a_failed_patch_keeps_the_previous_version(tool, workspace, manager):
    existing_skill(manager)
    _, data = await propose(tool, operation="update", name="demo", description="Second")
    proposals.commit(manager, "alice", data["proposal_id"])
    kept = workspace.skill_prev_get("demo")
    with pytest.raises(ValueError):
        manager.patch_user_skill("alice", "demo", "not present", "x")
    assert workspace.skill_prev_get("demo") == kept


def test_undo_without_a_previous_version(manager):
    existing_skill(manager)
    manager._ws("alice").skill_prev_del("demo")
    with pytest.raises(LookupError):
        manager.undo_last_change("alice", "demo")


def test_usage_counting_does_not_create_versions(manager, workspace):
    existing_skill(manager)
    workspace.skill_prev_del("demo")
    manager.increment_usage("alice", "demo")
    assert workspace.skill_prev_get("demo") is None


# --- endpoints ---------------------------------------------------------------------------------------


@pytest.fixture
def client(manager, monkeypatch):
    monkeypatch.setattr(router_module, "_get_manager", lambda: manager)
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/skills")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_confirm_endpoint_applies_the_staged_copy_not_the_request(tool, manager, client):
    _, data = await propose(tool, name="demo", description="Staged", instructions="Staged steps")
    async with client:
        listed = (await client.get("/api/skills/alice/proposals")).json()
        assert listed["count"] == 1 and listed["proposals"][0]["proposal_id"] == data["proposal_id"]
        response = await client.post(
            f"/api/skills/alice/proposals/{data['proposal_id']}/confirm",
            json={"name": "demo", "description": "Tampered", "instructions": "Tampered"},
        )
        assert response.status_code == 200 and response.json()["status"] == "confirmed"
        assert (
            await client.post(f"/api/skills/alice/proposals/{data['proposal_id']}/confirm")
        ).status_code == 404
    saved = manager.get_all_skills("alice")["demo"]
    assert saved.description == "Staged" and "Staged steps" in saved.instructions


@pytest.mark.asyncio
async def test_endpoint_status_codes_for_conflict_reject_and_undo(tool, manager, client):
    existing_skill(manager)
    _, update = await propose(tool, operation="update", name="demo", instructions="Rewritten")
    _, skipped = await propose(
        tool, operation="patch", name="demo", old_string="2. Check evidence.", new_string="2. Check twice."
    )
    manager.update_user_skill("alice", "demo", {"description": "Changed meanwhile"})
    async with client:
        assert (
            await client.post(f"/api/skills/alice/proposals/{update['proposal_id']}/confirm")
        ).status_code == 409
        assert (
            await client.delete(f"/api/skills/alice/proposals/{skipped['proposal_id']}")
        ).status_code == 200
        assert (
            await client.delete(f"/api/skills/alice/proposals/{skipped['proposal_id']}")
        ).status_code == 404
        assert (await client.post("/api/skills/alice/proposals/not-an-id/confirm")).status_code == 404
        assert (await client.delete("/api/skills/alice/demo")).status_code == 200
        restored = await client.post("/api/skills/alice/undo", json={"name": "demo"})
        assert restored.status_code == 200
        assert (
            await client.post("/api/skills/alice/undo", json={"name": "never-existed"})
        ).status_code == 404
    assert "demo" in manager.get_all_skills("alice")


# --- events -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_forwards_the_proposal_event_between_call_and_result(tool):
    from agents import Agent
    from agents.items import ToolCallItem, ToolCallOutputItem
    from agents.stream_events import RunItemStreamEvent
    from openai.types.responses import ResponseFunctionToolCall

    from core.runtime.events import _stream_sdk_events

    agent = Agent(name="offline")
    body = "Complete reference body\n" * 2500

    class Result:
        async def stream_events(self):
            yield RunItemStreamEvent(
                name="tool_called",
                item=ToolCallItem(
                    agent=agent,
                    raw_item=ResponseFunctionToolCall(
                        type="function_call", call_id="c1", name="skill", arguments="{}"
                    ),
                ),
            )
            output = await tool(
                SkillParams(
                    action="propose",
                    name="demo",
                    description="d",
                    instructions="Steps",
                    references={"r.md": body},
                )
            )
            yield RunItemStreamEvent(
                name="tool_output",
                item=ToolCallOutputItem(
                    agent=agent,
                    raw_item={"type": "function_call_output", "call_id": "c1", "output": output},
                    output=output,
                ),
            )

    events = [event async for event in _stream_sdk_events(Result(), [tool])]
    assert [event["code"] for event in events] == [4, 12, 5]
    assert json.loads(events[1]["content"])["references"]["r.md"] == body
    assert body not in events[2]["content"]
    assert tool.event_sink is None  # the binding is released when the stream ends


@pytest.mark.asyncio
async def test_delegation_delivers_the_proposal_event_and_counts_it_as_visible_output():
    from core.delegation import run_delegation

    proposal = {"code": 12, "content": json.dumps({"type": "skill_proposal", "proposal_id": "a" * 32})}

    class Runner:
        session_id = "child"
        config = SimpleNamespace(name="session-reviewer")

        async def run_stream(self, task, context):
            yield proposal
            yield {"code": 2, "content": "I proposed a skill; it waits for your confirmation."}

    frames = []

    async def emit(frame):
        frames.append(frame)

    receipt = await run_delegation(Runner(), "extract", "", "review", emit=emit)
    codes = [frame["code"] for frame in frames]
    assert 12 in codes and 2 in codes
    sent = next(frame for frame in frames if frame["code"] == 12)
    assert sent["task_id"] == "review" and sent["source_agent"] == "session-reviewer"
    assert 12 in receipt["delivered_codes"] and receipt["status"] == "delivered"
    assert "confirmation" in receipt["summary"] and "skill_proposal" not in receipt["summary"]
