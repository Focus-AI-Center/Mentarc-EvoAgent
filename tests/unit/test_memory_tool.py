"""Memory-file registration across code execution, indexing and persistence."""

import json

import pytest

from core.agent_compat import convert_tool
from core.plugins.internal import MemoryParams, MemoryTool, SkillTool
from core.skills import skill_from_md
from utils.memory_index import parse_memory_index
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", backend=LocalFSBackend(root=tmp_path))


def create_file(workspace, path="memory/customer_acme.md", text="ACME purchasing preferences"):
    target = workspace._backend.root / workspace.user_id / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


async def register(tool, **kwargs):
    values = dict(
        action="append",
        type="project",
        title="ACME",
        file_path="memory/customer_acme.md",
        summary="Purchasing preferences",
    )
    return await tool(MemoryParams(**(values | kwargs)))


@pytest.mark.asyncio
async def test_execute_code_register_then_read_in_new_session(workspace, monkeypatch):
    from core.plugins import execute_code as execute_module
    from core.plugins.sandbox import local

    monkeypatch.setattr(local, "WORKSPACE_ROOT", str(workspace._backend.root))
    monkeypatch.setattr(execute_module, "_BACKEND", local.LocalSubprocessBackend())
    execute = execute_module.ExecuteCodeTool(user_id="alice", session_id="session-1")
    payload = json.loads(
        await execute(
            execute_module.ExecuteCodeParams(
                language="python",
                code='from pathlib import Path; Path("memory").mkdir(); '
                'Path("memory/customer_acme.md").write_text("ACME purchasing preferences", encoding="utf-8")',
            )
        )
    )
    assert payload["success"] is True
    tool = MemoryTool(user_id="alice", workspace=workspace)
    assert "Registered" in await register(tool)
    index = workspace.read_prompt("MEMORY")
    assert "[ACME](memory/customer_acme.md)" in index
    assert "ACME" not in workspace.read_prompt("MEMORY_project")
    new_session = MemoryTool(user_id="alice", workspace=workspace)
    assert "ACME purchasing preferences" in await new_session(MemoryParams(action="get", title="ACME"))


@pytest.mark.asyncio
async def test_file_edits_are_read_without_reregistering(workspace):
    path = create_file(workspace)
    tool = MemoryTool(user_id="alice", workspace=workspace)
    await register(tool)
    await tool(MemoryParams(action="get", title="ACME"))
    path.write_text("Updated purchasing preferences", encoding="utf-8")
    result = await tool(MemoryParams(action="get", title="ACME"))
    assert "Updated purchasing preferences" in result
    index = workspace.read_prompt("MEMORY")
    assert "Registered" in await register(tool)
    assert workspace.read_prompt("MEMORY") == index


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../bob/private.md", "/etc/passwd", "memory/../../bob.md", "memory//x.md"])
async def test_rejects_paths_outside_workspace(workspace, path):
    tool = MemoryTool(user_id="alice", workspace=workspace)
    before = workspace.read_prompt("MEMORY")
    assert "Operation failed" in await register(tool, file_path=path)
    assert workspace.read_prompt("MEMORY") == before


@pytest.mark.asyncio
async def test_rejects_cross_user_symlink(workspace):
    other_user = workspace._backend.root / "bob"
    other_user.mkdir()
    (other_user / "private.md").write_text("private", encoding="utf-8")
    (workspace._backend.root / "alice" / "linked").symlink_to(other_user, target_is_directory=True)
    tool = MemoryTool(user_id="alice", workspace=workspace)
    assert "Operation failed" in await register(tool, file_path="linked/private.md")


@pytest.mark.asyncio
async def test_missing_empty_file_and_invalid_metadata_leave_index_unchanged(workspace):
    tool = MemoryTool(user_id="alice", workspace=workspace)
    before = workspace.read_prompt("MEMORY")
    assert "does not exist" in await register(tool)
    create_file(workspace, text="")
    assert "is empty" in await register(tool)
    create_file(workspace)
    assert "summary" in await register(tool, summary=None)
    assert "Do not pass content" in await register(tool, content="ambiguous body")
    assert "title" in await register(tool, title="bad\nindex line")
    assert workspace.read_prompt("MEMORY") == before


@pytest.mark.asyncio
async def test_duplicate_titles_across_types_require_type(workspace):
    create_file(workspace)
    create_file(workspace, "memory/background.md", "User background")
    tool = MemoryTool(user_id="alice", workspace=workspace)
    await register(tool)
    await register(tool, type="user", file_path="memory/background.md")
    assert "Specify type" in await tool(MemoryParams(action="get", title="ACME"))
    assert "User background" in await tool(MemoryParams(action="get", title="ACME", type="user"))
    before = workspace.read_prompt("MEMORY")
    assert "this title already exists" in await tool(
        MemoryParams(
            action="append",
            type="project",
            title="ACME",
            content="duplicate grouped entry",
        )
    )
    assert workspace.read_prompt("MEMORY") == before


@pytest.mark.asyncio
async def test_legacy_grouped_entries_survive_rebuild_alongside_file_links(workspace):
    create_file(workspace)
    tool = MemoryTool(user_id="alice", workspace=workspace)
    await tool(MemoryParams(action="append", type="feedback", title="Concise", content="Keep answers short"))
    await register(tool)
    await tool(MemoryParams(action="rebuild_index"))
    assert "Keep answers short" in await tool(MemoryParams(action="get", title="Concise"))
    assert "ACME purchasing preferences" in await tool(MemoryParams(action="get", title="ACME"))
    await tool(
        MemoryParams(
            action="rewrite",
            type="feedback",
            content="## Concise (2026-10-30)\nUpdated guidance",
        )
    )
    assert "[ACME](memory/customer_acme.md)" in workspace.read_prompt("MEMORY")
    assert "Updated guidance" in await tool(MemoryParams(action="get", title="Concise"))


@pytest.mark.asyncio
async def test_missing_file_reference_is_retained_on_rebuild(workspace):
    target = create_file(workspace)
    tool = MemoryTool(user_id="alice", workspace=workspace)
    await register(tool)
    target.unlink()
    await tool(MemoryParams(action="rebuild_index"))
    assert "customer_acme.md" in workspace.read_prompt("MEMORY")
    assert "does not exist" in await tool(MemoryParams(action="get", title="ACME"))


@pytest.mark.asyncio
async def test_encoded_paths_and_long_bodies(workspace):
    path = "memory/Customer (ACME).md"
    create_file(workspace, path, "€" * 900)
    tool = MemoryTool(user_id="alice", workspace=workspace)
    await register(tool, file_path=path)
    entry = parse_memory_index(workspace.read_prompt("MEMORY").split("---", 2)[-1])[0]
    assert entry.file_path == path
    result = await tool(MemoryParams(action="get", title="ACME"))
    assert "€" * 900 in result
    assert "Truncated" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [True, False])
@pytest.mark.parametrize("length", [8000, 8001])
async def test_long_memory_uses_bash_fallback_without_pagination(workspace, independent, length):
    body = "€" * length
    tool = MemoryTool(user_id="alice", workspace=workspace)
    if independent:
        create_file(workspace, text=body)
        await register(tool)
        expected_path = "memory/customer_acme.md"
    else:
        await tool(MemoryParams(action="append", type="project", title="ACME", content=body))
        expected_path = "prompts/MEMORY_project.md"
    result = await tool(MemoryParams(action="get", title="ACME"))
    assert "€" * 8000 in result
    if length > 8000:
        assert "€" * 8001 not in result
        assert "Truncated" in result
        assert "execute_code" in result
        assert "cat -- " + expected_path in result
    else:
        assert "Truncated" not in result
    schema = MemoryParams.model_json_schema()["properties"]
    assert "offset" not in schema and "max_chars" not in schema


@pytest.mark.asyncio
async def test_workspace_snapshot_restores_independent_memory_file(workspace):
    create_file(workspace)
    await register(MemoryTool(user_id="alice", workspace=workspace))
    snapshot = workspace.export_snapshot()
    workspace.import_snapshot(snapshot)
    result = await MemoryTool(user_id="alice", workspace=workspace)(MemoryParams(action="get", title="ACME"))
    assert "ACME purchasing preferences" in result


def test_exposed_tool_names_and_legacy_skill_references(workspace):
    memory = MemoryTool(user_id="alice", workspace=workspace)
    skill = SkillTool(user_id="alice", workspace=workspace, skill_manager=None)
    assert convert_tool(memory).name == "memory"
    assert convert_tool(skill).name == "skill"
    loaded = skill_from_md(
        "---\nname: legacy\ndescription: Legacy skill\nrequired_tools: [skill_manager, update_memory]\n---\n"
        'Use skill_manager(action="get") and update_memory(action="read").'
    )
    assert loaded.required_tools == ["skill", "memory"]
    assert "skill_manager" not in loaded.instructions
    assert "update_memory" not in loaded.instructions


def test_legacy_tool_history_is_migrated_without_changing_saved_records():
    from core.agent import _sanitize_history
    from utils.history_compressor import HistoryCompressor

    saved = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "skill_manager",
                        "arguments": '{"action":"get","name":"legacy"}',
                    }
                }
            ],
        },
        {"role": "tool", "name": "skill_manager", "content": "skill instructions"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "update_memory",
                        "arguments": '{"action":"read"}',
                    }
                }
            ],
        },
    ]
    migrated = _sanitize_history(saved)
    assert migrated[0]["tool_calls"][0]["function"]["name"] == "skill"
    assert migrated[1]["name"] == "skill"
    assert migrated[2]["tool_calls"][0]["function"]["name"] == "memory"
    assert saved[0]["tool_calls"][0]["function"]["name"] == "skill_manager"
    assert "legacy" in HistoryCompressor.extract_asset_index(saved)
    assert "legacy" in HistoryCompressor.extract_asset_index(migrated)
