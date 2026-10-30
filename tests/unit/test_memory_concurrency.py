"""Force stale reads across independent Agent/workspace instances."""

import asyncio
import threading

import pytest

from core.plugins.internal import MemoryParams, MemoryTool
from utils.memory_index import parse_memory_index
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace


class RendezvousBackend(LocalFSBackend):
    def __init__(self, root, barrier):
        super().__init__(root)
        self.barrier = barrier
        self.first_read = True

    def read_text_with_etag_sync(self, key):
        result = super().read_text_with_etag_sync(key)
        if key.endswith("/MEMORY.md") and self.first_read:
            self.first_read = False
            self.barrier.wait(timeout=10)
        return result


def concurrent_tools(tmp_path, count):
    barrier = threading.Barrier(count)
    return [
        MemoryTool(user_id="alice", workspace=Workspace("alice", RendezvousBackend(tmp_path, barrier)))
        for _ in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [True, False])
async def test_parallel_appends_preserve_all_bodies_and_index_entries(tmp_path, independent):
    count = 6
    tools = concurrent_tools(tmp_path, count)
    ws = tools[0]._ws
    ws.write_prompt("MEMORY", "---\nname: MEMORY\ndescription: preserved\n---\n")
    params = []
    for i in range(count):
        fields = {"content": f"Body {i}"}
        if independent:
            path = f"memory/topic-{i}.md"
            target = tmp_path / "alice" / path
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"Body {i}")
            fields = {"file_path": path, "summary": f"Topic {i}"}
        params.append(MemoryParams(action="append", type="project", title=f"Topic {i}", **fields))
    results = await asyncio.gather(*(tool(p) for tool, p in zip(tools, params, strict=False)))
    assert all("updated index" in result for result in results), results
    entries = parse_memory_index(ws.read_prompt("MEMORY"))
    assert {entry.title for entry in entries} == {f"Topic {i}" for i in range(count)}
    assert len(entries) == count
    assert "description: preserved" in ws.read_prompt("MEMORY")
    for i in range(count):
        assert f"Body {i}" in await tools[0](MemoryParams(action="get", title=f"Topic {i}"))


@pytest.mark.asyncio
async def test_parallel_registration_of_one_file_is_idempotent(tmp_path):
    tools = concurrent_tools(tmp_path, 4)
    path = tmp_path / "alice" / "note.md"
    path.write_text("Details")
    results = await asyncio.gather(
        *(
            tool(
                MemoryParams(
                    action="append", type="project", title=f"Title {i}", file_path="note.md", summary="Note"
                )
            )
            for i, tool in enumerate(tools)
        )
    )
    assert all("Registered" in result for result in results), results
    assert len(parse_memory_index(tools[0]._ws.read_prompt("MEMORY"))) == 1


@pytest.mark.asyncio
async def test_rebuild_racing_with_registration_preserves_file_link(tmp_path):
    register, rebuild = concurrent_tools(tmp_path, 2)
    (tmp_path / "alice" / "note.md").write_text("Details")
    register._ws.write_prompt("MEMORY_feedback", "## Concise (2026-10-30)\nKeep answers short")
    results = await asyncio.gather(
        register(
            MemoryParams(action="append", type="project", title="Note", file_path="note.md", summary="Note")
        ),
        rebuild(MemoryParams(action="rebuild_index")),
    )
    assert all("failed" not in result for result in results), results
    entries = parse_memory_index(register._ws.read_prompt("MEMORY"))
    assert {entry.title for entry in entries} == {"Concise", "Note"}


@pytest.mark.asyncio
async def test_rewrite_rejects_a_concurrent_body_change(tmp_path, monkeypatch):
    backend = LocalFSBackend(tmp_path)
    ws = Workspace("alice", backend)
    ws.write_prompt("MEMORY_project", "## Original (2026-10-30)\nOriginal body")
    write = backend.write_text_sync
    injected = False

    def competing_write(key, content, *, if_match=None):
        nonlocal injected
        if key.endswith("/MEMORY_project.md") and if_match is not None and not injected:
            injected = True
            write(key, ws.read_prompt("MEMORY_project") + "\n## Concurrent (2026-10-30)\nNew body")
        return write(key, content, if_match=if_match)

    monkeypatch.setattr(backend, "write_text_sync", competing_write)
    result = await MemoryTool(user_id="alice", workspace=ws)(
        MemoryParams(action="rewrite", type="project", content="## Replacement (2026-10-30)\nReplacement")
    )
    assert "changed by another operation" in result
    assert "New body" in ws.read_prompt("MEMORY_project")
    assert "Replacement" not in ws.read_prompt("MEMORY_project")
