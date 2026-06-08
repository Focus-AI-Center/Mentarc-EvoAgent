import pytest

from core.mcp.registry import load_mcp_registry


class FakeWorkspace:
    def __init__(self, data):
        self.data = data

    def mcp_config_load(self):
        return self.data


@pytest.mark.asyncio
async def test_mcp_registry_missing_returns_empty(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    reg = await load_mcp_registry(FakeWorkspace({}))
    assert reg.servers == ()


@pytest.mark.asyncio
async def test_mcp_registry_normalizes_http_and_redacts(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    reg = await load_mcp_registry(
        FakeWorkspace(
            {
                "version": 1,
                "servers": {
                    "docs": {
                        "enabled": True,
                        "transport": "http",
                        "url": "https://user:pass@example.com/mcp",
                        "headers": {"Authorization": "Bearer secret"},
                        "timeout": 999,
                    }
                },
            }
        )
    )
    assert len(reg.servers) == 1
    server = reg.servers[0]
    assert server.transport == "streamable-http"
    assert server.timeout == 120
    redacted = server.redacted()
    assert redacted["headers"] == {"Authorization": "***"}
    assert "user:pass" not in redacted["url"]


@pytest.mark.asyncio
async def test_mcp_registry_skips_stdio_in_local_mode(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    reg = await load_mcp_registry(
        FakeWorkspace(
            {
                "servers": {
                    "fs": {
                        "enabled": True,
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "server"],
                    }
                }
            }
        )
    )
    assert reg.servers == ()


@pytest.mark.asyncio
async def test_mcp_registry_accepts_stdio_in_minio_and_filters_env(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "http://minio:9000")
    reg = await load_mcp_registry(
        FakeWorkspace(
            {
                "servers": {
                    "fs": {
                        "enabled": True,
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "server"],
                        "env": {"TOKEN": "ok", "NODE_OPTIONS": "--require hack"},
                        "toolFilter": {"include": ["read_*"], "exclude": ["read_secret"]},
                    }
                }
            }
        )
    )
    assert len(reg.servers) == 1
    server = reg.servers[0]
    assert server.source_kind == "sandbox"
    assert server.env == {"TOKEN": "ok"}
    assert server.tool_filter.allows("read_file")
    assert not server.tool_filter.allows("write_file")
    assert not server.tool_filter.allows("read_secret")
