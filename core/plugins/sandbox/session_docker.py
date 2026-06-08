"""Session-scoped Docker sandbox for MinIO mode.

One long-lived container is shared by ``execute_code`` and stdio MCP for the
same ``(user_id, session_id)``. The container entrypoint mounts the user's
MinIO workspace at ``/workspace`` and then stays alive; individual code runs
and MCP servers are launched through Docker exec.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import socket
import time
from dataclasses import dataclass
from typing import Any

from utils.logger import logger

from . import SandboxResult, SandboxStartupError

_MAX_OUTPUT_CHARS = 10_000
_IDLE_TTL_S = int(os.getenv("SANDBOX_SESSION_IDLE_TTL", os.getenv("MCP_STDIO_IDLE_TTL", "900")))
_CONTAINER_TIMEOUT_S = int(os.getenv("SANDBOX_SESSION_CONTAINER_TIMEOUT", "3600"))


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) > _MAX_OUTPUT_CHARS:
        return s[:_MAX_OUTPUT_CHARS], True
    return s, False


def _extract_socket(obj):
    if hasattr(obj, "_sock"):
        return obj._sock
    if hasattr(obj, "sock"):
        return obj.sock
    return obj


def _session_fields(user_id: str, session_id: str) -> str:
    return f"user={user_id} session={session_id}"


def _drain_multiplexed(buffer: bytearray) -> tuple[list[tuple[int, bytes]], bytearray]:
    payloads: list[tuple[int, bytes]] = []
    while buffer:
        if len(buffer) >= 8 and buffer[0] in (1, 2):
            size = int.from_bytes(buffer[4:8], "big")
            if len(buffer) < 8 + size:
                break
            stream = buffer[0]
            payload = bytes(buffer[8:8 + size])
            del buffer[:8 + size]
            payloads.append((stream, payload))
            continue
        payloads.append((1, bytes(buffer)))
        buffer.clear()
    return payloads, buffer


@dataclass(frozen=True)
class SandboxSessionKey:
    user_id: str
    session_id: str


class DockerSandboxSession:
    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        client: Any,
        image: str,
        network: str,
        minio_endpoint: str,
        minio_access_key: str,
        minio_secret_key: str,
        minio_bucket: str,
    ):
        self.user_id = user_id
        self.session_id = session_id
        self._client = client
        self.image = image
        self.network = network
        self.minio_endpoint = minio_endpoint
        self.minio_access_key = minio_access_key
        self.minio_secret_key = minio_secret_key
        self.minio_bucket = minio_bucket
        self.last_used = time.monotonic()
        self.container = None
        self._closed = False
        self._exec_lock = asyncio.Lock()

    @property
    def container_id(self) -> str:
        if self.container is None:
            return ""
        return getattr(self.container, "short_id", "") or getattr(self.container, "id", "")[:12]

    async def start(self) -> None:
        if not self.minio_endpoint or not self.minio_access_key or not self.minio_secret_key:
            raise SandboxStartupError(
                "DockerSandboxSession requires MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY"
            )
        env = {
            "MINIO_ENDPOINT": self.minio_endpoint,
            "MINIO_ACCESS_KEY": self.minio_access_key,
            "MINIO_SECRET_KEY": self.minio_secret_key,
            "MINIO_BUCKET": self.minio_bucket,
            "USER_ID": self.user_id,
            "SANDBOX_SESSION_MODE": "1",
            "REL_CWD": "",
            "LANGUAGE": "bash",
            "TIMEOUT_S": str(max(_CONTAINER_TIMEOUT_S, _IDLE_TTL_S + 300)),
            "CODE": "sleep infinity",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        }
        mem_mb = int(os.getenv("SANDBOX_SESSION_MEMORY_MB", os.getenv("EXECUTE_CODE_MEMORY_MB", "512")))
        logger.info(
            "sandbox.session starting "
            f"{_session_fields(self.user_id, self.session_id)} image={self.image} network={self.network}"
        )
        self.container = await asyncio.to_thread(
            self._client.containers.run,
            image=self.image,
            environment=env,
            network_mode=self.network,
            cap_add=["SYS_ADMIN"],
            devices=["/dev/fuse:/dev/fuse:rwm"],
            security_opt=["apparmor=unconfined"],
            auto_remove=False,
            detach=True,
            stdin_open=True,
            tty=False,
            mem_limit=f"{mem_mb}m" if mem_mb > 0 else None,
            labels={
                "evoagent.kind": "session-sandbox",
                "evoagent.user_id": self.user_id,
                "evoagent.session_id": self.session_id,
            },
        )
        logger.info(
            "sandbox.session started "
            f"{_session_fields(self.user_id, self.session_id)} container={self.container_id}"
        )

    async def ensure_running(self) -> None:
        if self.container is None:
            await self.start()
            return
        try:
            await asyncio.to_thread(self.container.reload)
            if getattr(self.container, "status", "") != "running":
                raise RuntimeError(f"container status={self.container.status}")
        except Exception:
            await self.close()
            await self.start()

    def _exec_shell(self, *, cwd: str, code: str, language: str, timeout_s: int) -> str:
        target = "/workspace" + (f"/{cwd}" if cwd else "")
        mkdir_cd = f"mkdir -p {shlex.quote(target)} && cd {shlex.quote(target)}"
        if language == "python":
            inner = f"{mkdir_cd} && timeout --kill-after=5 {int(timeout_s)} python -c {shlex.quote(code)}"
        else:
            inner = f"{mkdir_cd} && timeout --kill-after=5 {int(timeout_s)} bash -c {shlex.quote(code)}"
        return (
            "runuser -u sandbox -- env "
            "HOME=/workspace LANG=C.UTF-8 LC_ALL=C.UTF-8 "
            "PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 "
            f"bash -lc {shlex.quote(inner)}"
        )

    async def run_code(
        self,
        *,
        cwd: str,
        code: str,
        language: str,
        timeout_s: int,
        memory_mb: int,
        network: bool,
    ) -> SandboxResult:
        self.last_used = time.monotonic()
        await self.ensure_running()
        cmd = self._exec_shell(cwd=cwd, code=code, language=language, timeout_s=timeout_s)
        logger.info(
            "sandbox.session exec_code "
            f"{_session_fields(self.user_id, self.session_id)} container={self.container_id} "
            f"lang={language} cwd={cwd or '.'} timeout={timeout_s}"
        )
        t0 = time.time()
        try:
            async with self._exec_lock:
                exec_id = await asyncio.to_thread(
                    self._client.api.exec_create,
                    self.container.id,
                    cmd=["bash", "-lc", cmd],
                    stdout=True,
                    stderr=True,
                    stdin=False,
                    tty=False,
                )
            output = await asyncio.wait_for(
                asyncio.to_thread(self._client.api.exec_start, exec_id["Id"], detach=False, tty=False, stream=False, demux=True),
                timeout=timeout_s + 30,
            )
            info = await asyncio.to_thread(self._client.api.exec_inspect, exec_id["Id"])
            return_code = int(info.get("ExitCode", -1))
        except asyncio.TimeoutError:
            return SandboxResult(
                return_code=124,
                stdout="",
                stderr=f"执行超时（>{timeout_s}s）",
                duration_ms=int((time.time() - t0) * 1000),
                sandbox_label=self.label,
                timed_out=True,
            )
        except Exception as err:
            logger.error(f"sandbox.session exec_code failed {_session_fields(self.user_id, self.session_id)}: {err}")
            return SandboxResult(
                return_code=-1,
                stdout="",
                stderr=f"sandbox session exec failure: {err}",
                duration_ms=int((time.time() - t0) * 1000),
                sandbox_label=self.label,
            )

        stdout_bytes, stderr_bytes = output if isinstance(output, tuple) else (output or b"", b"")
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        out, t1 = _truncate(stdout)
        err_text, t2 = _truncate(stderr)
        return SandboxResult(
            return_code=return_code,
            stdout=out,
            stderr=err_text,
            duration_ms=int((time.time() - t0) * 1000),
            sandbox_label=self.label,
            truncated=t1 or t2,
            timed_out=(return_code == 124),
        )

    @property
    def label(self) -> str:
        return f"docker-session({self.image},net={self.network},container={self.container_id})"

    async def exec_stdio_socket(self, *, server_name: str, command: str, args: tuple[str, ...], env: dict[str, str]):
        self.last_used = time.monotonic()
        await self.ensure_running()
        env_parts = [f"{k}={shlex.quote(v)}" for k, v in env.items()]
        env_prefix = "env " + " ".join(env_parts) + " " if env_parts else ""
        cmd = " ".join([shlex.quote(command), *(shlex.quote(a) for a in args)])
        inner = f"cd /workspace && exec {env_prefix}{cmd} 2>/tmp/mcp-{server_name}.stderr"
        shell = (
            "runuser -u sandbox -- env "
            "HOME=/workspace LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 "
            f"bash -lc {shlex.quote(inner)}"
        )
        logger.info(
            "sandbox.session exec_mcp "
            f"{_session_fields(self.user_id, self.session_id)} container={self.container_id} "
            f"server={server_name} command={command!r} args={list(args)!r} env_keys={sorted(env.keys())!r}"
        )
        exec_id = await asyncio.to_thread(
            self._client.api.exec_create,
            self.container.id,
            cmd=["bash", "-lc", shell],
            stdout=True,
            stderr=True,
            stdin=True,
            tty=False,
        )
        sock_obj = await asyncio.to_thread(
            self._client.api.exec_start,
            exec_id["Id"],
            detach=False,
            tty=False,
            stream=False,
            socket=True,
        )
        sock = _extract_socket(sock_obj)
        try:
            sock.settimeout(0.2)
        except Exception:
            pass
        return exec_id["Id"], sock

    async def close(self) -> None:
        self._closed = True
        if self.container is not None:
            cid = self.container_id
            logger.info(f"sandbox.session closing {_session_fields(self.user_id, self.session_id)} container={cid}")
            try:
                await asyncio.to_thread(self.container.remove, force=True, v=True)
            except Exception as exc:
                logger.warning(f"sandbox.session cleanup failed {_session_fields(self.user_id, self.session_id)}: {exc}")
            self.container = None


class SessionDockerBackend:
    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        minio_endpoint: str | None = None,
        minio_access_key: str | None = None,
        minio_secret_key: str | None = None,
        minio_bucket: str | None = None,
    ):
        self.image = image or os.getenv("SANDBOX_IMAGE", "evoagent-sandbox:python3.11")
        self.network = network or os.getenv("SANDBOX_NETWORK", "host")
        self.minio_endpoint = minio_endpoint or os.getenv("MINIO_ENDPOINT", "")
        self.minio_access_key = minio_access_key or os.getenv("MINIO_ACCESS_KEY", "")
        self.minio_secret_key = minio_secret_key or os.getenv("MINIO_SECRET_KEY", "")
        self.minio_bucket = minio_bucket or os.getenv("MINIO_BUCKET", "evoagent-workspaces")
        self._client = None
        self._sessions: dict[SandboxSessionKey, DockerSandboxSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    def _get_client(self):
        if self._client is None:
            try:
                import docker  # type: ignore
            except ImportError as err:
                raise SandboxStartupError(
                    "SessionDockerBackend requires the `docker` Python package; install with `pip install docker>=7`."
                ) from err
            try:
                self._client = docker.from_env()
                self._client.ping()
            except Exception as err:
                raise SandboxStartupError(
                    f"Cannot reach Docker daemon (DOCKER_HOST={os.getenv('DOCKER_HOST', '<local>')}): {err}"
                ) from err
        return self._client

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop(), name="sandbox-session-reaper")

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale: list[DockerSandboxSession] = []
            async with self._lock:
                for key, session in list(self._sessions.items()):
                    if now - session.last_used > _IDLE_TTL_S:
                        stale.append(session)
                        self._sessions.pop(key, None)
            await asyncio.gather(*(s.close() for s in stale), return_exceptions=True)

    async def get_session(self, *, user_id: str, session_id: str) -> DockerSandboxSession:
        self._ensure_reaper()
        key = SandboxSessionKey(user_id=user_id, session_id=session_id or "default")
        async with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                session.last_used = time.monotonic()
                logger.info(
                    "sandbox.session reusing "
                    f"{_session_fields(key.user_id, key.session_id)} container={session.container_id}"
                )
                return session
            session = DockerSandboxSession(
                user_id=key.user_id,
                session_id=key.session_id,
                client=self._get_client(),
                image=self.image,
                network=self.network,
                minio_endpoint=self.minio_endpoint,
                minio_access_key=self.minio_access_key,
                minio_secret_key=self.minio_secret_key,
                minio_bucket=self.minio_bucket,
            )
            self._sessions[key] = session
        try:
            await session.start()
            return session
        except Exception:
            async with self._lock:
                self._sessions.pop(key, None)
            await session.close()
            raise

    async def run(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        cwd: str,
        code: str,
        language: str,
        timeout_s: int,
        memory_mb: int,
        network: bool,
    ) -> SandboxResult:
        session = await self.get_session(user_id=user_id, session_id=session_id or "default")
        return await session.run_code(
            cwd=cwd,
            code=code,
            language=language,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            network=network,
        )

    async def exec_stdio_socket(
        self,
        *,
        user_id: str,
        session_id: str,
        server_name: str,
        command: str,
        args: tuple[str, ...],
        env: dict[str, str],
    ):
        session = await self.get_session(user_id=user_id, session_id=session_id)
        return await session.exec_stdio_socket(server_name=server_name, command=command, args=args, env=env)

    async def close_all(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)


_BACKEND: SessionDockerBackend | None = None


def get_session_docker_backend() -> SessionDockerBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = SessionDockerBackend()
    return _BACKEND


async def close_all_session_sandboxes() -> None:
    if _BACKEND is not None:
        await _BACKEND.close_all()


__all__ = [
    "DockerSandboxSession",
    "SessionDockerBackend",
    "SandboxSessionKey",
    "get_session_docker_backend",
    "close_all_session_sandboxes",
]
