from __future__ import annotations

import json

import pytest

from core.plugins import initialize_plugins
from core.plugins import execute_code as execute_code_module
from core.plugins.sandbox import SandboxResult


class FakeSessionBackend:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return SandboxResult(
            return_code=0,
            stdout="ok",
            stderr="",
            duration_ms=1,
            sandbox_label="fake-session",
        )


@pytest.mark.asyncio
async def test_execute_code_passes_session_id(monkeypatch):
    backend = FakeSessionBackend()
    monkeypatch.setattr(execute_code_module, "_BACKEND", backend)
    tool = initialize_plugins(["execute_code"], user_id="u1", session_id="s1")[0]

    payload = json.loads(await tool(tool.param_anno(code="print(1)", language="python")))

    assert payload["success"] is True
    assert backend.calls[0]["user_id"] == "u1"
    assert backend.calls[0]["session_id"] == "s1"
