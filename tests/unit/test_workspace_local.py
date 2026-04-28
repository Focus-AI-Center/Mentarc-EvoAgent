"""``Workspace`` tests against the LocalFS storage backend.

Same surface as before the M2 refactor — the public methods are sync and
their semantics shouldn't change just because the backend swapped from
direct ``Path`` writes to ``StorageBackend.*_sync`` calls.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def workspace(monkeypatch):
    """Fresh per-test workspace rooted under a tmp dir, LOCAL mode."""
    tmp = Path(tempfile.mkdtemp(prefix="ws_test_"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp))
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)

    # Reload anything that bound WORKSPACE_ROOT at import time.
    import importlib

    import configs.settings as s

    importlib.reload(s)
    import utils.storage_backend as sb

    sb.reset_backend()
    import utils.workspace as w

    importlib.reload(w)

    yield w.Workspace("alice")
    shutil.rmtree(tmp, ignore_errors=True)


# ── prompts ─────────────────────────────────────────────────────────────


def test_init_seeds_default_prompts(workspace):
    names = workspace.list_prompts()
    assert "SOUL" in names
    assert "USER" in names
    assert workspace.read_prompt("SOUL")  # non-empty


def test_write_then_read_prompt(workspace):
    workspace.write_prompt("MEMORY", "remembered")
    assert workspace.read_prompt("MEMORY") == "remembered"


def test_prompt_traversal_rejected(workspace):
    with pytest.raises(ValueError):
        workspace.read_prompt("../escape")


# ── skills ──────────────────────────────────────────────────────────────


def test_skill_set_get_del(workspace):
    workspace.skill_set("foo", "body")
    assert workspace.skill_get("foo") == "body"
    assert "foo" in workspace.skill_get_all()
    assert workspace.skill_del("foo")
    assert workspace.skill_get("foo") is None


def test_skill_nested_name(workspace):
    workspace.skill_set("a/b", "nested body")
    assert workspace.skill_get("a/b") == "nested body"
    workspace.skill_ref_set("a/b", "ex.md", "ref content")
    refs = workspace.skill_refs_get("a/b")
    assert refs == {"ex.md": "ref content"}


def test_init_seeds_builtin_skills(workspace):
    # configs/skills/skill-creator/ ships with the repo
    assert "skill-creator" in workspace.skill_get_all()


# ── sessions ────────────────────────────────────────────────────────────


def test_history_save_load(workspace):
    workspace.history_save(
        "s1",
        {
            "timestamp": "2026-04-28T00:00:00Z",
            "message_count": 1,
            "conversation": [{"role": "user", "content": "hi"}],
        },
    )
    loaded = workspace.history_load("s1")
    assert loaded is not None
    assert loaded["message_count"] == 1


def test_list_sessions(workspace):
    workspace.history_save(
        "s1",
        {
            "timestamp": "2026-04-28T00:00:00Z",
            "message_count": 1,
            "conversation": [{"role": "user", "content": "first"}],
        },
    )
    workspace.history_save(
        "s2",
        {
            "timestamp": "2026-04-28T01:00:00Z",
            "message_count": 2,
            "conversation": [{"role": "user", "content": "second"}],
        },
    )
    items = workspace.list_sessions()
    assert {it["session_id"] for it in items} == {"s1", "s2"}
    # Newest first
    assert items[0]["session_id"] == "s2"


def test_history_del(workspace):
    workspace.history_save("s1", {"timestamp": "x", "message_count": 0})
    assert workspace.history_del("s1")
    assert workspace.history_load("s1") is None


# ── images / previews ──────────────────────────────────────────────────


def test_image_roundtrip(workspace):
    workspace.image_write("a.png", b"\x89PNG\x00")
    assert workspace.image_read("a.png") == b"\x89PNG\x00"
    assert "a.png" in workspace.image_list()
    assert workspace.image_del("a.png")
    assert workspace.image_read("a.png") is None


def test_preview_roundtrip(workspace):
    workspace.preview_write("p1", "<html/>")
    assert workspace.preview_read("p1") == "<html/>"
    assert "p1" in workspace.preview_list()
    assert workspace.preview_del("p1")
    assert workspace.preview_read("p1") is None


# ── bulk / lifecycle ───────────────────────────────────────────────────


def test_clear_sessions(workspace):
    workspace.history_save("s1", {"timestamp": "x"})
    workspace.history_save("s2", {"timestamp": "y"})
    assert workspace.clear_sessions() == 2
    assert workspace.list_sessions() == []


def test_delete_workspace(workspace):
    workspace.write_prompt("MEMORY", "m")
    workspace.image_write("a.png", b"\x00")
    workspace.history_save("s1", {"timestamp": "x"})
    cleared = workspace.delete_workspace()
    assert cleared["sessions"] >= 1
    assert cleared["images"] >= 1


def test_reset_to_defaults(workspace):
    workspace.write_prompt("MEMORY", "modified")
    workspace.reset_to_defaults()
    # Defaults re-seeded; per-user MEMORY was wiped → defaults should reappear
    assert "SOUL" in workspace.list_prompts()
