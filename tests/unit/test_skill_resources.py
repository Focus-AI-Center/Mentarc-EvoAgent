"""Template resources retain their paths through sync, execution and backup."""

import pytest

import utils.workspace as workspace_module
from core.plugins.sandbox import local
from utils.storage_backend.localfs import LocalFSBackend


@pytest.fixture
def template_workspace(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    skill = templates / "demo"
    for directory in ("scripts", "assets", "references", "child/scripts"):
        (skill / directory).mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\nRun python scripts/run.py")
    (skill / "scripts/run.py").write_text(
        'from pathlib import Path\nprint(Path("assets/data.bin").read_bytes().hex())\n'
    )
    (skill / "assets/data.bin").write_bytes(b"\x00\xff\x80asset")
    (skill / "references/rules.md").write_text("Detailed rules")
    (skill / "references/manual.bin").write_bytes(b"\xff\xfeBinary reference")
    (skill / "child/SKILL.md").write_text("---\nname: child\ndescription: child\n---\nChild steps")
    (skill / "child/scripts/child.py").write_text("print('child')")
    monkeypatch.setattr(workspace_module, "_DEFAULTS_SKILLS_DIR", templates)
    monkeypatch.setattr(workspace_module, "_SYNC_VERSION", workspace_module._compute_sync_version())
    backend = LocalFSBackend(tmp_path / "workspace")
    return workspace_module.Workspace("alice", backend), templates


@pytest.mark.asyncio
async def test_synced_script_runs_with_documented_cwd_and_binary_asset(template_workspace, monkeypatch):
    ws, _ = template_workspace
    root = ws._backend.root / ws.user_id / "skills/demo"
    assert (root / "scripts/run.py").is_file()
    assert not (root / "references/scripts/run.py").exists()
    assert ws.skill_refs_get("demo") == {"rules.md": "Detailed rules"}
    monkeypatch.setattr(local, "WORKSPACE_ROOT", str(ws._backend.root))
    result = await local.LocalSubprocessBackend().run(
        user_id="alice",
        cwd="skills/demo",
        code="python scripts/run.py",
        language="bash",
        timeout_s=10,
        memory_mb=256,
        network=False,
    )
    assert result.return_code == 0, result.stderr
    assert result.stdout.strip() == b"\x00\xff\x80asset".hex()


def test_snapshot_preserves_resources_and_excludes_nested_skill_from_parent(template_workspace, monkeypatch):
    ws, templates = template_workspace
    snapshot = ws.export_snapshot()
    parent = snapshot["skills"]["demo"]
    assert set(parent["resources"]) == {"scripts/run.py", "assets/data.bin", "references/manual.bin"}
    assert parent["refs"] == {"rules.md": "Detailed rules"}
    assert set(snapshot["skills"]["demo/child"]["resources"]) == {"scripts/child.py"}
    # Restore without the template tree so template seeding cannot mask data loss.
    monkeypatch.setattr(workspace_module, "_DEFAULTS_SKILLS_DIR", templates / "missing")
    restored = workspace_module.Workspace("bob", ws._backend)
    restored.import_snapshot(snapshot)
    assert restored.skill_resources_get("demo") == ws.skill_resources_get("demo")
    assert restored.skill_refs_get("demo") == ws.skill_refs_get("demo")
    assert restored.skill_resources_get("demo/child") == ws.skill_resources_get("demo/child")


def test_layout_version_migrates_old_resource_paths_and_preserves_memory(template_workspace):
    ws, _ = template_workspace
    ws.write_prompt("MEMORY", "User memory")
    ws.write_prompt("USER", "User profile")
    ws.skill_ref_set("demo", "scripts/run.py", "old misplaced copy")
    ws._backend.write_text_sync("alice/.meta/sync_version", "legacy-layout")
    migrated = workspace_module.Workspace("alice", ws._backend)
    assert not (ws._backend.root / "alice/skills/demo/references/scripts/run.py").exists()
    assert "scripts/run.py" in migrated.skill_resources_get("demo")
    assert migrated.read_prompt("MEMORY") == "User memory"
    assert migrated.read_prompt("USER") == "User profile"


def test_old_snapshots_without_resources_remain_importable(template_workspace):
    ws, _ = template_workspace
    ws.import_snapshot(
        {"skills": {"old": {"content": "Old skill", "refs": {"old.md": "Old reference"}}}}, replace=False
    )
    assert ws.skill_get("old") == "Old skill"
    assert ws.skill_refs_get("old") == {"old.md": "Old reference"}
