"""Metrics decorator for ``SandboxBackend``.

Wraps a backend so each ``run()`` increments the active gauge, observes
duration into a labelled histogram on completion, and decrements on exit
(success or exception). The decorator deliberately does not catch backend
exceptions — the dispatcher upstream sees the original exception, just with
metrics already updated.

Outcome derivation:
  - ``timed_out`` flag set         → ``"timeout"`` (rc 124 from coreutils)
  - ``return_code == 137``         → ``"oom"``     (SIGKILL from cgroup OOM)
  - ``return_code != 0``           → ``"error"``
  - ``return_code == 0``           → ``"ok"``
  - backend raised before result   → ``"error"`` (observed below)
"""

from __future__ import annotations

import time
from typing import Any

from utils.metrics import SANDBOX_ACTIVE, SANDBOX_EXEC_DURATION

from . import SandboxBackend, SandboxResult


def _outcome(result: SandboxResult) -> str:
    if result.timed_out:
        return "timeout"
    if result.return_code == 137:
        return "oom"
    if result.return_code != 0:
        return "error"
    return "ok"


class MetricsSandboxBackend:
    """Wraps any ``SandboxBackend`` with Prometheus instrumentation.

    Implements the same ``run()`` signature as the protocol, so the
    dispatcher in ``execute_code`` cannot tell whether it has a wrapped or
    unwrapped backend.
    """

    def __init__(self, inner: SandboxBackend, label: str):
        self._inner = inner
        self._label = label

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def run(self, **kwargs: Any) -> SandboxResult:
        backend_label = self._label
        language = kwargs.get("language", "?")
        SANDBOX_ACTIVE.labels(backend=backend_label).inc()
        start = time.monotonic()
        try:
            result = await self._inner.run(**kwargs)
        except Exception:
            SANDBOX_EXEC_DURATION.labels(backend=backend_label, language=language, outcome="error").observe(
                time.monotonic() - start
            )
            SANDBOX_ACTIVE.labels(backend=backend_label).dec()
            raise

        SANDBOX_ACTIVE.labels(backend=backend_label).dec()
        # Prefer the backend's reported duration (closer to inner timing); fall
        # back to wall-clock if the backend didn't fill it in.
        duration_s = result.duration_ms / 1000.0 if result.duration_ms else time.monotonic() - start
        SANDBOX_EXEC_DURATION.labels(
            backend=backend_label, language=language, outcome=_outcome(result)
        ).observe(duration_s)
        return result


__all__ = ["MetricsSandboxBackend"]
