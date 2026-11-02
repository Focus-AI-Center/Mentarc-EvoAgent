"""Exact skill edits through the SDK tool and conditional workspace storage."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agents.run_context import RunContextWrapper

from core.agent_compat import convert_tool
from core.plugins.internal import SkillParams, SkillTool
from core.skills import Skill
from core.skills.manager import SkillManager
from utils.storage_backend.localfs import LocalFSBackend
from utils.storage_backend.minio_sdk import MinIOSDKBackend
from utils.workspace import Workspace


@pytest.fixture
def runtime(tmp_path):
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    manager = SkillManager(workspace)
    manager.save_user_skill(
        "alice",
        Skill(
            name="demo",
            description="Keep the original description",
            instructions="1. Confirm inputs.\n2. Check evidence.\n3. Write the report.",
            required_tools=["fetch_url"],
            references={"rules.md": "Keep source links.\nMark uncertain claims.", "other.md": "Keep me."},
            source="extracted",
            extracted_from="origin",
            version=7,
            usage_count=12,
        ),
    )
    tool = SkillTool(user_id="alice", workspace=workspace, skill_manager=manager)
    return workspace, tool


@pytest.mark.asyncio
async def test_sdk_patch_preserves_metadata_other_text_and_resources(runtime):
    workspace, tool = runtime
    workspace.skill_resource_set("demo", "scripts/run.py", b"print('unchanged')\n")
    before = workspace.skill_get("demo")
    refs = workspace.skill_refs_get("demo")
    sdk_tool = convert_tool(tool)
    assert "patch" in sdk_tool.params_json_schema["properties"]["action"]["enum"]
    result = await sdk_tool.on_invoke_tool(
        RunContextWrapper(context=None),
        json.dumps(
            {
                "action": "patch",
                "name": "demo",
                "old_string": "2. Check evidence.",
                "new_string": "2. Check evidence and keep source links.",
            }
        ),
    )
    assert result.startswith("Patched main instructions")
    assert workspace.skill_get("demo") == before.replace(
        "2. Check evidence.", "2. Check evidence and keep source links."
    )
    assert workspace.skill_refs_get("demo") == refs
    assert workspace.skill_resources_get("demo")["scripts/run.py"] == b"print('unchanged')\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["nested/rules.md", "references/nested/rules.md"])
async def test_reference_patch_supports_nested_paths_deletion_and_unicode(runtime, filename):
    workspace, tool = runtime
    original = "# Rules\n\nKeep évidence — " + chr(0x4E00) + "\nRemove this.\n"
    workspace.skill_ref_set("demo", "nested/rules.md", original)
    main = workspace.skill_get("demo")
    result = await tool(
        SkillParams(
            action="patch", name="demo", ref_filename=filename, old_string="Remove this.\n", new_string=""
        )
    )
    assert result.startswith("Patched reference 'nested/rules.md'")
    assert workspace.skill_refs_get("demo")["nested/rules.md"] == original.replace("Remove this.\n", "")
    assert workspace.skill_get("demo") == main
    assert workspace.skill_refs_get("demo")["other.md"] == "Keep me."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"old_string": "absent", "new_string": "replacement"}, "was not found"),
        ({"old_string": "report", "new_string": "replacement"}, "more than once"),
        ({"old_string": "", "new_string": "replacement"}, "requires"),
        ({"old_string": "report"}, "explicit new_string"),
        ({"old_string": "report", "new_string": None}, "explicit new_string"),
        ({"new_string": "replacement"}, "requires"),
        ({"old_string": "Check evidence.", "new_string": "Check evidence."}, "must differ"),
        (
            {"old_string": "description: Keep the original description", "new_string": "changed"},
            "was not found",
        ),
    ],
)
async def test_invalid_patch_never_writes(runtime, fields, reason):
    workspace, tool = runtime
    workspace.skill_set("demo", workspace.skill_get("demo") + "Another report\n")
    before = workspace.skill_get("demo")
    refs = workspace.skill_refs_get("demo")
    result = await tool(SkillParams(action="patch", name="demo", **fields))
    assert reason in result
    assert workspace.skill_get("demo") == before
    assert workspace.skill_refs_get("demo") == refs


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_main_patch_ignores_frontmatter_matches_and_preserves_format(runtime, newline):
    workspace, _ = runtime
    header = "---\nname: demo\ndescription: old text\ncustom_key: keep\n---\n".replace("\n", newline)
    body = "\n    old text\n\nKeep whitespace.  \n".replace("\n", newline)
    workspace.skill_set("demo", header + body)
    workspace.skill_patch("demo", "old text", "new text")
    assert workspace.skill_get("demo") == header + body.replace("old text", "new text")


def test_overlapping_matches_are_ambiguous_and_plain_documents_work(runtime):
    workspace, _ = runtime
    workspace.skill_set("demo", "aaa")
    with pytest.raises(ValueError, match="more than once"):
        workspace.skill_patch("demo", "aa", "new")
    assert workspace.skill_get("demo") == "aaa"
    workspace.skill_patch("demo", "aaa", "plain instructions")
    assert workspace.skill_get("demo") == "plain instructions"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename", ["../escape.md", "/escape.md", "references/../escape.md", "C:\\escape.md"]
)
async def test_reference_patch_rejects_unsafe_paths(runtime, filename):
    workspace, tool = runtime
    refs = workspace.skill_refs_get("demo")
    result = await tool(
        SkillParams(
            action="patch", name="demo", ref_filename=filename, old_string="Keep me.", new_string="bad"
        )
    )
    assert result.startswith("Operation failed:")
    assert workspace.skill_refs_get("demo") == refs


@pytest.mark.asyncio
@pytest.mark.parametrize("name,filename", [("missing", ""), ("missing", "rules.md"), ("demo", "missing.md")])
async def test_missing_targets_are_not_created(runtime, name, filename):
    workspace, tool = runtime
    keys = workspace._backend.list_keys_sync("alice/")
    result = await tool(
        SkillParams(action="patch", name=name, ref_filename=filename, old_string="old", new_string="new")
    )
    assert "does not exist" in result
    assert workspace._backend.list_keys_sync("alice/") == keys


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["", "rules.md"])
async def test_concurrent_edit_survives_conflicting_patch(runtime, monkeypatch, filename):
    workspace, tool = runtime
    key = f"alice/skills/demo/{'references/' + filename if filename else 'SKILL.md'}"
    backend = workspace._backend
    original = backend.read_text_sync(key)
    concurrent = original + "\nA concurrent update.\n"
    write = backend.write_text_sync

    def race(target, content, *, if_match=None):
        assert target == key and if_match is not None
        write(key, concurrent)
        return write(target, content, if_match=if_match)

    monkeypatch.setattr(backend, "write_text_sync", race)
    result = await tool(
        SkillParams(
            action="patch",
            name="demo",
            ref_filename=filename,
            old_string="Keep source links." if filename else "2. Check evidence.",
            new_string="Replacement",
        )
    )
    assert "changed while patching" in result
    assert backend.read_text_sync(key) == concurrent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_string,expected", [("2. Check evidence.", "Patched"), ("stale text", "Operation failed")]
)
async def test_patch_allows_reloading_after_success_or_stale_match(runtime, old_string, expected):
    workspace, tool = runtime
    await tool(SkillParams(action="get", name="demo"))
    assert "demo" in tool._got_skills
    result = await tool(
        SkillParams(action="patch", name="demo", old_string=old_string, new_string="New step.")
    )
    assert result.startswith(expected)
    assert "demo" not in tool._got_skills
    current = await tool(SkillParams(action="get", name="demo"))
    assert workspace.skill_get("demo") in current


def test_minio_patch_sends_etag_with_write_and_does_not_use_sandbox(runtime, monkeypatch):
    workspace, _ = runtime
    backend = MinIOSDKBackend("http://localhost:9000", "test-access", "test-secret")
    response = Mock(headers={"etag": '"original-etag"'})
    response.read.return_value = b"Read old evidence.\n"
    backend._client.get_object = Mock(return_value=response)
    backend._client._execute = Mock(return_value=SimpleNamespace(headers={"etag": '"new-etag"'}))
    monkeypatch.setattr(workspace, "_backend", backend)
    workspace.skill_patch("demo", "old evidence", "new evidence")
    backend._client.get_object.assert_called_once_with("evoagent-workspaces", "alice/skills/demo/SKILL.md")
    call = backend._client._execute.call_args
    assert call.args[0] == "PUT"
    assert call.kwargs["headers"]["If-Match"] == '"original-etag"'
    assert call.kwargs["body"] == b"Read new evidence.\n"
