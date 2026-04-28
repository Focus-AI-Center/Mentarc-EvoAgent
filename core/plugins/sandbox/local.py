"""Local sandbox: bubblewrap or plain subprocess on the same host as the server.

Ported verbatim from the original ``execute_code.py`` body — same flag set,
same fall-back behaviour, same env contract. Wrapped in the async
``SandboxBackend`` interface so the dispatcher can swap it for the remote
Docker backend without execute_code knowing the difference.

Resolves bwrap availability lazily on first call (rather than at import) so
tests / dev hosts without bwrap don't break import; the first invocation
caches the resolved mode (``off`` or ``bubblewrap``) for the process lifetime.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from configs.settings import WORKSPACE_ROOT
from utils.logger import logger

from . import SandboxResult

_MAX_OUTPUT_CHARS = 10_000

_SANDBOX_MODE = os.getenv("EXECUTE_CODE_SANDBOX", "auto").lower()
_SANDBOX_MEMORY_MB_DEFAULT = int(os.getenv("EXECUTE_CODE_MEMORY_MB", "512"))


def _resolved_sandbox_mode() -> str:
    """Resolve ``EXECUTE_CODE_SANDBOX`` to ``off`` or ``bubblewrap`` once."""
    if _SANDBOX_MODE in ("off", "none", "disabled", "false", "0"):
        return "off"
    has_bwrap = (
        platform.system() == "Linux" and shutil.which("bwrap") is not None
    )
    if _SANDBOX_MODE == "bubblewrap":
        if not has_bwrap:
            logger.warning(
                "EXECUTE_CODE_SANDBOX=bubblewrap requested but `bwrap` is not "
                "available; falling back to unsandboxed subprocess."
            )
            return "off"
        return "bubblewrap"
    return "bubblewrap" if has_bwrap else "off"


def _user_workspace_root(cwd: Path) -> Path:
    """Walk ``cwd`` up to the per-user directory under ``WORKSPACE_ROOT``."""
    ws_root = Path(WORKSPACE_ROOT).resolve()
    try:
        rel_parts = cwd.resolve().relative_to(ws_root).parts
    except ValueError:
        return cwd
    if not rel_parts:
        return cwd
    return ws_root / rel_parts[0]


def _build_bwrap_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: int,
    network: bool,
    memory_mb: int,
) -> list[str]:
    user_ws = _user_workspace_root(cwd)
    args: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup",
    ]
    args += ["--share-net"] if network else ["--unshare-net"]
    for p in (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib32",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/resolv.conf",
        "/etc/nsswitch.conf",
        "/etc/hosts",
    ):
        args += ["--ro-bind-try", p, p]
    args += ["--ro-bind-try", sys.prefix, sys.prefix]
    if sys.base_prefix != sys.prefix:
        args += ["--ro-bind-try", sys.base_prefix, sys.base_prefix]
    args += ["--bind", str(user_ws), str(user_ws)]
    args += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--chdir", str(cwd),
    ]
    for k, v in env.items():
        args += ["--setenv", k, str(v)]
    full = args + list(cmd)
    if memory_mb > 0 or timeout > 0:
        limits = ["prlimit"]
        if memory_mb > 0:
            limits.append(f"--as={memory_mb * 1024 * 1024}")
        if timeout > 0:
            limits.append(f"--cpu={timeout}")
        full = limits + full
    return full


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) > _MAX_OUTPUT_CHARS:
        return s[:_MAX_OUTPUT_CHARS], True
    return s, False


class LocalBwrapBackend:
    """``SandboxBackend`` running on the same host as the server."""

    def __init__(self):
        self._mode_cache: str | None = None

    def _mode(self) -> str:
        if self._mode_cache is None:
            self._mode_cache = _resolved_sandbox_mode()
        return self._mode_cache

    async def run(
        self,
        *,
        user_id: str,
        cwd: str,
        code: str,
        language: str,
        timeout_s: int,
        memory_mb: int,
        network: bool,
    ) -> SandboxResult:
        ws_dir = (Path(WORKSPACE_ROOT).resolve() / user_id)
        ws_dir.mkdir(parents=True, exist_ok=True)
        cwd_path = (ws_dir / cwd).resolve() if cwd else ws_dir
        # Defense in depth: dispatcher already strips ../, but a misuse of the
        # backend directly should not let the sandbox cd outside the user's tree.
        try:
            cwd_path.relative_to(ws_dir)
        except ValueError:
            return SandboxResult(
                return_code=-1,
                stdout="",
                stderr=f"cwd escapes user workspace: {cwd_path}",
                duration_ms=0,
                sandbox_label="rejected",
            )
        cwd_path.mkdir(parents=True, exist_ok=True)

        if language == "python":
            cmd = [sys.executable, "-c", code]
        else:
            cmd = ["bash", "-c", code]

        system_path = os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        env = {
            "HOME": str(cwd_path),
            "PATH": f"{sys.prefix}/bin:{system_path}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }

        mode = self._mode()
        if mode == "bubblewrap":
            full_cmd = _build_bwrap_command(
                cmd,
                cwd_path,
                env,
                timeout=timeout_s,
                network=network,
                memory_mb=memory_mb if memory_mb > 0 else _SANDBOX_MEMORY_MB_DEFAULT,
            )
            run_kwargs: dict = {}
            sandbox_label = (
                f"bubblewrap(network={'on' if network else 'off'},"
                f"mem={memory_mb if memory_mb > 0 else _SANDBOX_MEMORY_MB_DEFAULT}MB)"
            )
        else:
            full_cmd = cmd
            run_kwargs = {"cwd": str(cwd_path), "env": env}
            sandbox_label = "none"

        logger.debug(
            f"sandbox.local user={user_id} lang={language} mode={sandbox_label} cwd={cwd_path}"
        )

        t0 = time.time()
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s + 5,
                **run_kwargs,
            )
        except subprocess.TimeoutExpired as err:
            return SandboxResult(
                return_code=-1,
                stdout=(err.stdout.decode("utf-8", "replace") if isinstance(err.stdout, bytes) else (err.stdout or "")),
                stderr=f"执行超时（>{timeout_s}s）",
                duration_ms=int((time.time() - t0) * 1000),
                sandbox_label=sandbox_label,
                timed_out=True,
            )
        except Exception as err:
            return SandboxResult(
                return_code=-1,
                stdout="",
                stderr=str(err),
                duration_ms=int((time.time() - t0) * 1000),
                sandbox_label=sandbox_label,
            )

        stdout, t1 = _truncate(result.stdout or "")
        stderr, t2 = _truncate(result.stderr or "")
        return SandboxResult(
            return_code=result.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.time() - t0) * 1000),
            sandbox_label=sandbox_label,
            truncated=t1 or t2,
        )


__all__ = ["LocalBwrapBackend"]
