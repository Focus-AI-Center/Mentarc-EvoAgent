"""Behaviour tests for ``LocalSubprocessBackend`` (sandbox in LOCAL mode).

Exercises the public ``run()`` contract: stdout, stderr, return_code,
timed_out, sandbox_label, and the cwd-escape rejection path.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def isolated_workspace(monkeypatch):
    """Per-test temporary WORKSPACE_ROOT to avoid polluting ``data/workspace``."""
    tmp = Path(tempfile.mkdtemp(prefix="sandbox_local_"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp))
    # Reload the module so it picks up the new WORKSPACE_ROOT.
    import importlib

    import configs.settings as s

    importlib.reload(s)
    import core.plugins.sandbox.local as m

    importlib.reload(m)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def backend(isolated_workspace):
    from core.plugins.sandbox.local import LocalSubprocessBackend

    return LocalSubprocessBackend()


# ── basic exec ──────────────────────────────────────────────────────────


async def test_bash_echo(backend):
    r = await backend.run(
        user_id="alice",
        cwd="",
        code="echo hello",
        language="bash",
        timeout_s=5,
        memory_mb=0,
        network=True,
    )
    assert r.return_code == 0
    assert "hello" in r.stdout
    assert r.duration_ms >= 0


async def test_python_print(backend):
    r = await backend.run(
        user_id="alice",
        cwd="",
        code="print(1 + 2)",
        language="python",
        timeout_s=5,
        memory_mb=0,
        network=True,
    )
    assert r.return_code == 0
    assert r.stdout.strip() == "3"


async def test_returncode_nonzero(backend):
    r = await backend.run(
        user_id="alice",
        cwd="",
        code="exit 42",
        language="bash",
        timeout_s=5,
        memory_mb=0,
        network=True,
    )
    assert r.return_code == 42


# ── isolation ───────────────────────────────────────────────────────────


async def test_cwd_is_user_workspace(backend, isolated_workspace):
    r = await backend.run(
        user_id="alice",
        cwd="",
        code="pwd",
        language="bash",
        timeout_s=5,
        memory_mb=0,
        network=True,
    )
    assert r.return_code == 0
    assert (isolated_workspace / "alice").as_posix() in r.stdout


async def test_workspace_writes_visible_to_host(backend, isolated_workspace):
    r = await backend.run(
        user_id="alice",
        cwd="",
        code="echo data > probe.txt",
        language="bash",
        timeout_s=5,
        memory_mb=0,
        network=True,
    )
    assert r.return_code == 0
    assert (isolated_workspace / "alice" / "probe.txt").read_text().strip() == "data"


async def test_traversal_rejected(backend):
    # Defense in depth: backend should refuse cwd that escapes user root,
    # even though the dispatcher already strips ``..``.
    r = await backend.run(
        user_id="alice",
        cwd="../../etc",
        code="pwd",
        language="bash",
        timeout_s=5,
        memory_mb=0,
        network=True,
    )
    assert r.return_code == -1
    assert "escapes user workspace" in r.stderr


# ── timeout ─────────────────────────────────────────────────────────────


async def test_timeout_kills_process(backend):
    r = await backend.run(
        user_id="alice",
        cwd="",
        code="sleep 10",
        language="bash",
        timeout_s=1,
        memory_mb=0,
        network=True,
    )
    assert r.return_code != 0
    assert r.timed_out
