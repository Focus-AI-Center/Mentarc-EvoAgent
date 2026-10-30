"""Local sandbox: plain subprocess on the same host as the server.

Used in LOCAL mode (``MINIO_ENDPOINT`` unset). Single-tenant, trusted-host
only — there is no kernel-level isolation. The dispatcher in
``execute_code.py`` still enforces command pattern blacklist, cwd
confinement, timeout, and output truncation; this backend just runs the
process under the user's workspace directory.

For multi-tenant or untrusted use, switch to MINIO mode (which routes
``execute_code`` to ``RemoteDockerBackend`` — a one-shot container per
invocation).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from configs.settings import WORKSPACE_ROOT
from utils.logger import logger

from . import SandboxResult

_MAX_OUTPUT_CHARS = 10_000


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) > _MAX_OUTPUT_CHARS:
        return s[:_MAX_OUTPUT_CHARS], True
    return s, False


class LocalSubprocessBackend:
    """``SandboxBackend`` running ``subprocess.run`` on the server host."""

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
        ws_dir = Path(WORKSPACE_ROOT).resolve() / user_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        cwd_path = (ws_dir / cwd).resolve() if cwd else ws_dir
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

        system_path = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env = {
            "HOME": str(cwd_path),
            "PATH": f"{sys.prefix}/bin:{system_path}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }

        sandbox_label = "subprocess"
        logger.debug(f"sandbox.local user={user_id} lang={language} cwd={cwd_path}")

        t0 = time.time()
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s + 5,
                cwd=str(cwd_path),
                env=env,
            )
        except subprocess.TimeoutExpired as err:
            return SandboxResult(
                return_code=-1,
                stdout=(
                    err.stdout.decode("utf-8", "replace")
                    if isinstance(err.stdout, bytes)
                    else (err.stdout or "")
                ),
                stderr=f"Execution timed out (>{timeout_s}s)",
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


__all__ = ["LocalSubprocessBackend"]
