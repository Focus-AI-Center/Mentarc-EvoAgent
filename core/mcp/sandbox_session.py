"""Session-scoped stdio MCP runtime inside the MinIO Docker sandbox.

MCP stdio keeps a long-lived JSON-RPC process/socket per
``(user_id, session_id, server_name)``. These processes and ``execute_code``
share the Docker container owned by ``(user_id, session_id)``; idle cleanup
of an MCP connection is separate from cleanup of the shared container.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from dataclasses import dataclass

from core.mcp.registry import MCPServerConfig, is_minio_mode
from core.plugins.sandbox.session_docker import get_session_docker_backend
from utils.logger import logger

_PROTOCOL_VERSION = "2024-11-05"
_IDLE_TTL_S = int(os.getenv("MCP_STDIO_IDLE_TTL", "900"))
_CONTAINER_TIMEOUT_S = int(os.getenv("MCP_STDIO_CONTAINER_TIMEOUT", "3600"))
_MAX_OUTPUT_CHARS = 20_000


def _log_session_fields(user_id: str, session_id: str, server_name: str) -> str:
    return f"user={user_id} session={session_id} server={server_name}"


def _content_to_jsonable(content: list) -> list[dict]:
    out: list[dict] = []
    for block in content or []:
        if isinstance(block, dict):
            out.append(block)
        else:
            out.append({"type": "text", "text": str(block)})
    return out


def _tool_to_jsonable(tool: dict) -> dict:
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description") or "",
        "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"},
    }


def _build_code(server: MCPServerConfig) -> str:
    env_parts = [f"{k}={shlex.quote(v)}" for k, v in server.env.items()]
    cmd = " ".join([shlex.quote(server.command), *(shlex.quote(a) for a in server.args)])
    env_prefix = "env " + " ".join(env_parts) + " " if env_parts else ""
    # Keep MCP stdout clean; server logs go to a file inside the sandbox.
    return f"cd /workspace && exec {env_prefix}{cmd} 2>/tmp/mcp-{server.name}.stderr"


def _extract_socket(obj):
    if hasattr(obj, "_sock"):
        return obj._sock
    if hasattr(obj, "sock"):
        return obj.sock
    return obj


@dataclass(frozen=True)
class _SessionKey:
    user_id: str
    session_id: str
    server_name: str


class SandboxStdioSession:
    def __init__(self, *, user_id: str, session_id: str, server: MCPServerConfig):
        self.user_id = user_id
        self.session_id = session_id
        self.server = server
        self.last_used = time.monotonic()
        self._client = None
        self._container = None
        self._sock = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._buffer = bytearray()
        self._closed = False
        self._lock = asyncio.Lock()
        self._multiplexed = True
        self._exec_id = ""

    async def start(self) -> None:
        if not is_minio_mode():
            raise RuntimeError("stdio MCP sandbox is only available in MinIO mode")

        backend = get_session_docker_backend()
        logger.info(
            "[mcp-sandbox] starting stdio MCP exec "
            f"{_log_session_fields(self.user_id, self.session_id, self.server.name)} "
            f"workdir=/workspace command={self.server.command!r} args={list(self.server.args)!r} "
            f"env_keys={sorted(self.server.env.keys())!r} timeout_s={self.server.timeout}"
        )
        self._exec_id, self._sock = await backend.exec_stdio_socket(
            user_id=self.user_id,
            session_id=self.session_id,
            server_name=self.server.name,
            command=self.server.command,
            args=self.server.args,
            env=self.server.env,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-stdio:{self.user_id}:{self.session_id}:{self.server.name}"
        )
        await self._initialize()
        logger.info(
            "[mcp-sandbox] stdio MCP initialized "
            f"{_log_session_fields(self.user_id, self.session_id, self.server.name)} "
            f"exec={self._exec_id}"
        )

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Mentarc-EvoAgent", "version": "1.0"},
            },
            timeout=15,
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                chunk = await asyncio.to_thread(self._sock.recv, 4096)
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._closed:
                    logger.warning(f"[mcp-sandbox] read loop failed server={self.server.name}: {exc}")
                break
            if not chunk:
                break
            self._buffer.extend(chunk)
            for payload in self._drain_stdout_payloads():
                self._handle_stdout_payload(payload)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("stdio MCP sandbox disconnected"))
        self._pending.clear()

    def _drain_stdout_payloads(self) -> list[bytes]:
        """Extract stdout payloads from Docker attach output.

        Containers run with ``tty=False`` so Docker frames stdout/stderr as
        8-byte headers plus payload. Keep a raw fallback for environments that
        return undecorated bytes.
        """
        payloads: list[bytes] = []
        while self._buffer:
            if len(self._buffer) >= 8 and self._buffer[0] in (1, 2):
                size = int.from_bytes(self._buffer[4:8], "big")
                if len(self._buffer) < 8 + size:
                    break
                stream = self._buffer[0]
                payload = bytes(self._buffer[8 : 8 + size])
                del self._buffer[: 8 + size]
                if stream == 1:
                    payloads.append(payload)
                elif payload:
                    logger.debug(
                        f"[mcp-sandbox] stderr server={self.server.name}: {payload[:200].decode('utf-8', 'replace')}"
                    )
                continue
            if b"\n" not in self._buffer:
                break
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            payloads.append(bytes(line + b"\n"))
        return payloads

    def _handle_stdout_payload(self, payload: bytes) -> None:
        for line in payload.splitlines():
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                logger.debug(f"[mcp-sandbox] ignored non-json stdout server={self.server.name}: {text[:200]}")
                continue
            req_id = msg.get("id")
            if req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    fut.set_result(msg)

    async def _send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        await asyncio.to_thread(self._sock.sendall, data)

    async def _request(self, method: str, params: dict | None = None, *, timeout: float) -> dict:
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._pending[req_id] = fut
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False))
        return msg.get("result") or {}

    async def list_tools(self) -> list[dict]:
        self.last_used = time.monotonic()
        logger.info(
            f"[mcp-sandbox] list_tools {_log_session_fields(self.user_id, self.session_id, self.server.name)}"
        )
        result = await self._request("tools/list", {}, timeout=10)
        tools = [_tool_to_jsonable(t) for t in result.get("tools", []) if isinstance(t, dict)]
        logger.info(
            "[mcp-sandbox] list_tools result "
            f"{_log_session_fields(self.user_id, self.session_id, self.server.name)} "
            f"count={len(tools)} tools={[t.get('name') for t in tools]!r}"
        )
        return tools

    async def call_tool(self, tool: str, args: dict | None, timeout: float) -> dict:
        self.last_used = time.monotonic()
        logger.info(
            "[mcp-sandbox] call_tool "
            f"{_log_session_fields(self.user_id, self.session_id, self.server.name)} "
            f"tool={tool} timeout={timeout}"
        )
        result = await self._request("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout)
        content = _content_to_jsonable(result.get("content") or [])
        text_len = sum(len(str(x)) for x in content)
        if text_len > _MAX_OUTPUT_CHARS:
            content = [
                {
                    "type": "text",
                    "text": json.dumps(content, ensure_ascii=False)[:_MAX_OUTPUT_CHARS] + "\n[truncated]",
                }
            ]
        return {"content": content, "is_error": bool(result.get("isError") or result.get("is_error"))}

    async def close(self) -> None:
        self._closed = True
        logger.info(
            "[mcp-sandbox] closing stdio MCP exec "
            f"{_log_session_fields(self.user_id, self.session_id, self.server.name)} "
            f"exec={self._exec_id}"
        )
        if self._reader_task:
            self._reader_task.cancel()
        try:
            if self._sock:
                await asyncio.to_thread(self._sock.close)
        except Exception:
            pass


_sessions: dict[_SessionKey, SandboxStdioSession] = {}
_sessions_lock = asyncio.Lock()
_reaper_task: asyncio.Task | None = None


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        stale: list[tuple[_SessionKey, SandboxStdioSession]] = []
        async with _sessions_lock:
            for key, sess in list(_sessions.items()):
                if now - sess.last_used > _IDLE_TTL_S:
                    stale.append((key, sess))
                    _sessions.pop(key, None)
        for _, sess in stale:
            await sess.close()


def _ensure_reaper() -> None:
    global _reaper_task
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.create_task(_reaper_loop(), name="mcp-stdio-reaper")


async def get_sandbox_stdio_session(
    *, user_id: str, session_id: str, server: MCPServerConfig
) -> SandboxStdioSession:
    _ensure_reaper()
    key = _SessionKey(user_id=user_id, session_id=session_id, server_name=server.name)
    async with _sessions_lock:
        sess = _sessions.get(key)
        if sess is not None:
            sess.last_used = time.monotonic()
            logger.info(
                f"[mcp-sandbox] reusing stdio sandbox {_log_session_fields(user_id, session_id, server.name)}"
            )
            return sess
        sess = SandboxStdioSession(user_id=user_id, session_id=session_id, server=server)
        _sessions[key] = sess
    try:
        await sess.start()
        return sess
    except Exception:
        async with _sessions_lock:
            _sessions.pop(key, None)
        await sess.close()
        raise


async def close_all_sandbox_stdio_sessions() -> None:
    global _reaper_task
    if _reaper_task:
        _reaper_task.cancel()
        _reaper_task = None
    async with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)


__all__ = ["get_sandbox_stdio_session", "close_all_sandbox_stdio_sessions", "SandboxStdioSession"]
