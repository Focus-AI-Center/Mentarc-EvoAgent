"""MCP registry loading and validation.

The registry lives in the user's workspace at ``.meta/mcp.json`` and is
intentionally read-only from this module: first version has no management API.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from utils.logger import logger
from utils.ws_call import ws_call

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SUPPORTED_TRANSPORTS = {"streamable-http", "http", "sse", "stdio"}
_DANGEROUS_ENV = {
    "NODE_OPTIONS",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "RUBYOPT",
    "PERL5OPT",
    "SHELLOPTS",
}
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120


def is_minio_mode() -> bool:
    return bool(os.getenv("MINIO_ENDPOINT", "").strip())


def _clamp_timeout(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _DEFAULT_TIMEOUT
    return min(max(n, 1), _MAX_TIMEOUT)


def _normalize_transport(value: Any) -> str:
    transport = str(value or "streamable-http").strip().lower()
    if transport == "http":
        return "streamable-http"
    return transport


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            netloc = "***@" + netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<redacted-url>"


def redact_mapping(mapping: dict[str, Any] | None) -> dict[str, str]:
    return {str(k): "***" for k in (mapping or {})}


@dataclass(frozen=True)
class MCPToolFilter:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: Any) -> MCPToolFilter:
        if not isinstance(raw, dict):
            return cls()
        include = tuple(str(x) for x in raw.get("include") or [] if isinstance(x, str))
        exclude = tuple(str(x) for x in raw.get("exclude") or [] if isinstance(x, str))
        return cls(include=include, exclude=exclude)

    def allows(self, tool_name: str) -> bool:
        if self.include and not any(fnmatch.fnmatch(tool_name, pat) for pat in self.include):
            return False
        if self.exclude and any(fnmatch.fnmatch(tool_name, pat) for pat in self.exclude):
            return False
        return True


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    enabled: bool = True
    timeout: int = _DEFAULT_TIMEOUT
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    tool_filter: MCPToolFilter = field(default_factory=MCPToolFilter)

    @property
    def source_kind(self) -> str:
        return "sandbox" if self.transport == "stdio" else "server"

    def redacted(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "transport": self.transport,
            "timeout": self.timeout,
        }
        if self.url:
            data["url"] = _redact_url(self.url)
        if self.headers:
            data["headers"] = redact_mapping(self.headers)
        if self.command:
            data["command"] = self.command
            data["args"] = list(self.args)
        if self.env:
            data["env"] = redact_mapping(self.env)
        if self.tool_filter.include or self.tool_filter.exclude:
            data["toolFilter"] = {
                "include": list(self.tool_filter.include),
                "exclude": list(self.tool_filter.exclude),
            }
        return data


def _clean_env(raw: Any, *, server_name: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key)
        if k in _DANGEROUS_ENV:
            logger.warning(f"[mcp-registry] dropping dangerous env var for {server_name}: {k}")
            continue
        out[k] = str(value)
    return out


def _parse_server(name: str, raw: Any, *, minio_mode: bool) -> MCPServerConfig | None:
    if not _NAME_RE.match(name):
        logger.warning(f"[mcp-registry] skipping invalid server name: {name!r}")
        return None
    if not isinstance(raw, dict):
        logger.warning(f"[mcp-registry] skipping non-object server config: {name}")
        return None

    enabled = bool(raw.get("enabled", True))
    transport = _normalize_transport(raw.get("transport"))
    if transport not in _SUPPORTED_TRANSPORTS:
        logger.warning(f"[mcp-registry] skipping {name}: unsupported transport={transport!r}")
        return None
    if transport == "stdio" and not minio_mode:
        logger.warning(f"[mcp-registry] skipping stdio MCP {name}: only enabled in MinIO mode")
        return None

    timeout = _clamp_timeout(raw.get("timeout"))
    tool_filter = MCPToolFilter.from_raw(raw.get("toolFilter") or raw.get("tool_filter"))

    if transport in ("streamable-http", "sse"):
        url = str(raw.get("url") or "").strip()
        if not url:
            logger.warning(f"[mcp-registry] skipping {name}: missing url")
            return None
        headers = (
            {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
            if isinstance(raw.get("headers"), dict)
            else {}
        )
        return MCPServerConfig(
            name=name,
            transport=transport,
            enabled=enabled,
            timeout=timeout,
            url=url,
            headers=headers,
            tool_filter=tool_filter,
        )

    command = str(raw.get("command") or "").strip()
    if not command:
        logger.warning(f"[mcp-registry] skipping stdio MCP {name}: missing command")
        return None
    args_raw = raw.get("args") or []
    if not isinstance(args_raw, list):
        logger.warning(f"[mcp-registry] skipping stdio MCP {name}: args must be a list")
        return None
    return MCPServerConfig(
        name=name,
        transport="stdio",
        enabled=enabled,
        timeout=timeout,
        command=command,
        args=tuple(str(x) for x in args_raw),
        env=_clean_env(raw.get("env"), server_name=name),
        tool_filter=tool_filter,
    )


@dataclass(frozen=True)
class MCPRegistry:
    version: int = 1
    servers: tuple[MCPServerConfig, ...] = ()

    @property
    def enabled_servers(self) -> tuple[MCPServerConfig, ...]:
        return tuple(s for s in self.servers if s.enabled)

    def redacted(self) -> dict[str, Any]:
        return {"version": self.version, "servers": {s.name: s.redacted() for s in self.servers}}


async def load_mcp_registry(workspace) -> MCPRegistry:
    """Load and validate ``.meta/mcp.json`` from the user's workspace."""
    try:
        raw = await ws_call(workspace, "mcp_config_load")
    except Exception as exc:
        logger.warning(f"[mcp-registry] failed to load mcp config: {exc}")
        return MCPRegistry()
    if not raw:
        return MCPRegistry()
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(f"[mcp-registry] invalid .meta/mcp.json: {exc}")
            return MCPRegistry()
    elif isinstance(raw, dict):
        data = raw
    else:
        logger.warning(f"[mcp-registry] invalid registry type: {type(raw).__name__}")
        return MCPRegistry()

    servers_raw = data.get("servers") if isinstance(data, dict) else None
    try:
        version = int(data.get("version", 1)) if isinstance(data, dict) else 1
    except (TypeError, ValueError):
        version = 1
    if not isinstance(servers_raw, dict):
        return MCPRegistry(version=version)

    minio = is_minio_mode()
    servers: list[MCPServerConfig] = []
    for name, cfg in servers_raw.items():
        parsed = _parse_server(str(name), cfg, minio_mode=minio)
        if parsed is not None:
            servers.append(parsed)
    return MCPRegistry(version=version, servers=tuple(servers))
