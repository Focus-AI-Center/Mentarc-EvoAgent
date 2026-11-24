"""Concurrent workers wait for one stdio MCP initialization before using a session."""

import asyncio

import pytest

import core.mcp.sandbox_session as runtime
from core.mcp.registry import MCPServerConfig


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(runtime, "_sessions", {})
    monkeypatch.setattr(runtime, "_sessions_lock", asyncio.Lock())
    monkeypatch.setattr(runtime, "_ensure_reaper", lambda: None)
    return dict(
        user_id="alice",
        session_id="parent",
        server=MCPServerConfig(name="files", transport="stdio", command="python"),
    )


@pytest.mark.asyncio
async def test_parallel_callers_do_not_receive_an_uninitialized_session(registry, monkeypatch):
    started, ready = asyncio.Event(), asyncio.Event()
    calls = []

    async def start(session):
        calls.append(session)
        started.set()
        await ready.wait()

    monkeypatch.setattr(runtime.SandboxStdioSession, "start", start)
    first = asyncio.create_task(runtime.get_sandbox_stdio_session(**registry))
    await started.wait()
    second = asyncio.create_task(runtime.get_sandbox_stdio_session(**registry))
    await asyncio.sleep(0)
    assert not second.done() and len(calls) == 1
    ready.set()
    assert (await first) is (await second)
    assert (await runtime.get_sandbox_stdio_session(**registry)) is calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_initialization_unblocks_peers_and_can_retry(registry, monkeypatch, cancel):
    started, fail = asyncio.Event(), asyncio.Event()
    sessions = []

    async def start(session):
        sessions.append(session)
        started.set()
        await fail.wait()
        raise RuntimeError("Offline initialization failure")

    monkeypatch.setattr(runtime.SandboxStdioSession, "start", start)
    first = asyncio.create_task(runtime.get_sandbox_stdio_session(**registry))
    await started.wait()
    second = asyncio.create_task(runtime.get_sandbox_stdio_session(**registry))
    await asyncio.sleep(0)
    if cancel:
        first.cancel()
    else:
        fail.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await first
    with pytest.raises(RuntimeError, match="initialization did not complete"):
        await asyncio.wait_for(second, 1)
    assert not runtime._sessions and sessions[0]._closed

    async def retry(session):
        sessions.append(session)

    monkeypatch.setattr(runtime.SandboxStdioSession, "start", retry)
    replacement = await runtime.get_sandbox_stdio_session(**registry)
    assert replacement is not sessions[0] and replacement._ready.is_set()
