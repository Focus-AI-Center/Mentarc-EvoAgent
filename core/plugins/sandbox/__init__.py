"""Pluggable sandbox backends for the ``execute_code`` tool.

Two implementations:

- ``LocalSubprocessBackend`` — plain ``subprocess.run`` on the same host as
  the server, scoped to the user's workspace directory. Used in LOCAL mode
  (``MINIO_ENDPOINT`` empty). Single-tenant / trusted-host only.

- ``SessionDockerBackend`` — one long-lived container per ``(user_id, session_id)``
  in MINIO mode. The container mounts the bucket via s3fs once, then
  ``execute_code`` and stdio MCP servers run through Docker exec in that same
  session sandbox.

Selection happens at startup via the same ``MINIO_ENDPOINT`` switch as the
storage backend (the two are paired — a LOCAL storage with a remote sandbox,
or vice versa, doesn't make sense and is rejected at startup).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of a single sandbox invocation.

    ``stdout`` / ``stderr`` are already truncated by the backend to the cap
    enforced by the ``execute_code`` plugin (10 000 chars by default); the
    ``truncated`` flag tells the dispatcher whether to add a "truncated" note
    to the user-visible payload.
    """

    return_code: int
    stdout: str
    stderr: str
    duration_ms: int
    sandbox_label: str
    truncated: bool = False
    timed_out: bool = False


class SandboxError(Exception):
    """Base for sandbox backend errors that don't map to a SandboxResult."""


class SandboxStartupError(SandboxError):
    """Raised when the backend cannot be brought up (image missing, daemon
    unreachable, etc.). Distinct from a sandbox that *ran* and returned
    non-zero — those are returned as a normal ``SandboxResult``."""


@runtime_checkable
class SandboxBackend(Protocol):
    """Async sandbox runner.

    Implementations must be safe to call concurrently from different
    coroutines (each invocation gets its own sandbox).
    """

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
        """Execute ``code`` in a sandbox scoped to ``user_id``.

        In LOCAL mode this remains a subprocess. In MINIO mode v1 uses a
        session container keyed by ``(user_id, session_id)`` so ``execute_code``
        and stdio MCP share runtime state.
        """


def make_sandbox_backend() -> SandboxBackend:
    """Factory: pick the sandbox backend based on environment.

    The choice is paired with the storage backend (selected by
    ``MINIO_ENDPOINT``):

    - ``MINIO_ENDPOINT`` unset    → ``LocalSubprocessBackend``
    - ``MINIO_ENDPOINT`` set      → session Docker backend per ``SANDBOX_BACKEND``
                                    (``docker`` default; ``k8s`` rejected
                                    until M5).

    Cross-mode configurations (LocalFS storage + remote sandbox, or MinIO
    storage + local sandbox) are forbidden — the two halves share state and
    have to agree on where the workspace lives.
    """
    minio_endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    sandbox_backend = os.getenv("SANDBOX_BACKEND", "").strip().lower()

    if not minio_endpoint:
        if sandbox_backend in ("docker", "k8s"):
            raise RuntimeError(
                f"SANDBOX_BACKEND={sandbox_backend!r} requires MINIO_ENDPOINT to be set "
                f"(LOCAL mode pairs storage=LocalFS with sandbox=subprocess; cross-mode is forbidden)."
            )
        from .local import LocalSubprocessBackend
        from .metrics import MetricsSandboxBackend

        return MetricsSandboxBackend(LocalSubprocessBackend(), label="local")

    # MINIO mode
    chosen = sandbox_backend or "docker"
    if chosen == "k8s":
        raise RuntimeError(
            "SANDBOX_BACKEND=k8s is reserved for M5 and not implemented yet — "
            "use SANDBOX_BACKEND=docker (default) for now."
        )
    if chosen != "docker":
        raise RuntimeError(f"Unknown SANDBOX_BACKEND={chosen!r}. Supported: 'docker' (default).")
    from .metrics import MetricsSandboxBackend
    from .session_docker import get_session_docker_backend

    return MetricsSandboxBackend(get_session_docker_backend(), label="docker-session")


__all__ = [
    "SandboxBackend",
    "SandboxError",
    "SandboxResult",
    "SandboxStartupError",
    "make_sandbox_backend",
]
