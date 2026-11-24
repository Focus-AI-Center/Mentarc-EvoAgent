"""Workspace agent definitions use the same catalog for discovery and execution."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from minio.error import S3Error

from core.agent import EvoAgent
from core.agent_registry import agent_catalog, load_agent_config, parse_agent_config
from core.sub_agent import SubAgentRunner
from utils.storage_backend.localfs import LocalFSBackend
from utils.storage_backend.minio_sdk import MinIOSDKBackend
from utils.workspace import Workspace


def definition(name, body="Follow the task.", **metadata):
    import yaml

    return "---\n" + yaml.safe_dump({"name": name, **metadata}) + "---\n" + body


@pytest.fixture(params=["local", "minio"])
def workspace(request, tmp_path):
    if request.param == "local":
        backend = LocalFSBackend(tmp_path)
    else:
        # Exercise the real SDK adapter with an in-memory object transport.
        backend = MinIOSDKBackend("http://localhost:9000", "test-access", "test-secret")
        objects = {}

        def missing(key):
            return S3Error(None, "NoSuchKey", "Missing", key, "request", "host")

        def get(bucket, key):
            if key not in objects:
                raise missing(key)
            return Mock(read=Mock(return_value=objects[key]), headers={"etag": '"test"'})

        def put(bucket, key, data, length, **kwargs):
            objects[key] = data.read(length)
            return SimpleNamespace(etag="test")

        def stat(bucket, key):
            if key not in objects:
                raise missing(key)
            return SimpleNamespace(etag="test")

        backend._client.get_object = get
        backend._client.put_object = put
        backend._client.stat_object = stat
        backend._client.list_objects = lambda bucket, prefix, **kwargs: [
            SimpleNamespace(object_name=key) for key in sorted(objects) if key.startswith(prefix)
        ]
        backend._client.remove_object = lambda bucket, key: objects.pop(key, None)

        def remove(bucket, items):
            for item in items:
                objects.pop(item.name, None)
            return iter(())

        backend._client.remove_objects = remove
        backend._client._http.urlopen = Mock(side_effect=AssertionError("Unexpected HTTP in an offline test"))
    return Workspace("alice", backend)


def test_templates_are_seeded_and_workspace_edits_survive_resync(workspace):
    assert workspace.agent_get("worker") and workspace.agent_get("session-reviewer")
    custom = definition("worker", "Custom worker rules.", execution_mode="worker")
    workspace.agent_set("worker", custom)
    workspace._backend.write_text_sync("alice/.meta/sync_version", "old-version")
    reloaded = Workspace("alice", workspace._backend)
    assert load_agent_config("worker", reloaded).body == "Custom worker rules."
    other = Workspace("bob", workspace._backend)
    assert load_agent_config("worker", other).body != "Custom worker rules."


def test_user_defined_types_override_defaults_and_invalid_override_is_not_revived(workspace):
    workspace.agent_set("analyst", definition("analyst", "Analyze files.", description="User analyst"))
    assert agent_catalog(workspace)["analyst"].description == "User analyst"
    workspace.agent_set("session-reviewer", "---\nname: wrong-name\n---\nInvalid override.")
    assert "session-reviewer" not in agent_catalog(workspace)
    with pytest.raises(ValueError):
        load_agent_config("session-reviewer", workspace)


@pytest.mark.parametrize("name", ["../other", "/absolute", "a/b", "", "a\\b"])
def test_agent_names_cannot_escape_their_directory(workspace, name):
    with pytest.raises(ValueError):
        load_agent_config(name, workspace)


def test_defaults_remain_a_fallback_when_a_workspace_object_is_missing(workspace):
    workspace.agent_get = lambda name: None
    assert load_agent_config("session-reviewer", workspace).name == "session-reviewer"


def test_unreadable_default_agent_does_not_break_workspace_or_catalog(workspace, tmp_path, monkeypatch):
    import core.agent_registry as registry
    import utils.workspace as storage

    defaults = tmp_path / "default-agents"
    defaults.mkdir()
    (defaults / "valid.md").write_text(definition("valid", "Analyze input."), encoding="utf-8")
    (defaults / "bad.md").write_bytes(b"\xff\xfe")
    (defaults / "directory.md").mkdir()
    monkeypatch.setattr(registry, "DEFAULT_AGENTS_DIR", str(defaults))
    monkeypatch.setattr(storage, "_DEFAULTS_AGENTS_DIR", defaults)
    monkeypatch.setattr(storage, "_SYNC_VERSION", "unreadable-agent-templates-test")
    fresh = Workspace("charlie", workspace._backend)
    configs = agent_catalog(fresh)
    assert "valid" in configs and "bad" not in configs and "directory" not in configs
    assert fresh.agent_get("valid") and fresh.agent_get("bad") is None


@pytest.mark.asyncio
async def test_catalog_is_refreshed_each_turn_and_execution_uses_that_snapshot(workspace, monkeypatch):
    import core.sub_agent as sub

    workspace.agent_set("analyst", definition("analyst", "First version.", description="First"))
    parent = EvoAgent("alice", "main", plugins=[], workspace=workspace, system_prompt="Offline")
    await parent.load_session()
    tool = next(tool for tool in parent.tools if tool.name == "sub_agent")
    assert "analyst: First" in tool.description
    workspace.agent_set("analyst", definition("analyst", "Second version.", description="Second"))
    seen = []

    class Child:
        def __init__(self, **kwargs):
            seen.append(kwargs["agent_config"].body)
            self.config = kwargs["agent_config"]
            self.session_id = "child"

        async def run_stream(self, *args):
            yield {"code": 2, "content": "Done"}

    monkeypatch.setattr(sub, "SubAgentRunner", Child)
    from core.plugins.internal import SubAgentParams

    await tool.delegate(SubAgentParams(agent_type="analyst", task="Analyze"))
    assert seen == ["First version."]
    await parent.load_session()
    assert "analyst: Second" in tool.description


@pytest.mark.asyncio
async def test_http_catalog_is_user_scoped_and_worker_profiles_have_no_direct_chat(workspace, monkeypatch):
    from app.api.routers import agent, sub_agent

    workspace.agent_set("analyst", definition("analyst", "Analyze.", description="Only Alice"))
    monkeypatch.setattr(agent, "_create_workspace", AsyncMock(return_value=workspace))
    app = FastAPI()
    app.include_router(sub_agent.router, prefix="/api/sub-agent")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/sub-agent/types", params={"userId": "alice"})
        types = {item["name"]: item for item in response.json()["types"]}
        assert types["analyst"]["description"] == "Only Alice"
        assert types["worker"]["execution_mode"] == "worker"
        response = await client.post(
            "/api/sub-agent/worker/chat",
            json={
                "userId": "alice",
                "sessionId": "main",
                "data": {"input": "Run"},
            },
        )
        assert response.status_code == 404


def test_snapshots_preserve_agent_definitions_and_binary_worker_outputs(tmp_path):
    workspace = Workspace("alice", LocalFSBackend(tmp_path))
    workspace.agent_set("analyst", definition("analyst", "Unique rules."))
    workspace._write_bytes("alice/workers/task/run/result.bin", b"\xff\x00")
    workspace.history_save("child", {"execution_mode": "worker", "conversation": []})
    assert not workspace.list_sessions()
    snapshot = workspace.export_snapshot()
    assert "child" in snapshot["sessions"]["records"]
    other = Workspace("bob", workspace._backend)
    other.import_snapshot(snapshot, replace=False)
    assert load_agent_config("analyst", other).body == "Unique rules."
    assert other._read_bytes("bob/workers/task/run/result.bin") == b"\xff\x00"
    assert json.dumps(snapshot)


def test_profile_cannot_relax_worker_runtime_limits(tmp_path):
    ws = Workspace("alice", LocalFSBackend(tmp_path))
    config = parse_agent_config(
        "worker",
        definition(
            "worker",
            execution_mode="worker",
            timeout=3600,
            max_steps=100,
            required_tools=["ask_user_question", "update_user_profile", "execute_code"],
        ),
    )
    runner = SubAgentRunner(
        config, "alice", "task", workspace=ws, internal_worker=True, output_dir="workers/task/run"
    )
    assert runner.config.timeout == 300 and runner.config.max_steps == 30
    assert {tool.name for tool in runner._build_tools()}.isdisjoint(
        {"ask_user_question", "update_user_profile"}
    )
