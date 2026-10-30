"""Code execution plugin — runs bash or Python inside the user's workspace.

This module is now a thin dispatcher: input validation + safety checks +
delegation to a ``SandboxBackend`` selected at startup by
``core.plugins.sandbox.make_sandbox_backend``.

Two backends exist (chosen automatically by the storage mode — see
``core/plugins/sandbox/__init__.py``):

  - ``LocalSubprocessBackend`` (LOCAL mode, ``MINIO_ENDPOINT`` empty) — plain
    ``subprocess.run`` scoped to the user's workspace directory. Single-tenant
    / trusted-host only; no kernel-level isolation.

  - ``SessionDockerBackend`` (MINIO mode, ``MINIO_ENDPOINT`` set) — one
    long-lived container per ``(user_id, session_id)``. ``execute_code`` and
    stdio MCP servers run in that same container via Docker exec.

Validation kept at this layer (backend-agnostic):

  - Reject obviously destructive patterns (``rm -rf /``, ``mkfs``, fork bomb…).
  - Clamp timeout to ``[1, 120]``.
  - Confine ``cwd`` to a relative subpath under the user's workspace
    (no leading ``/``, no ``..``).
  - Truncate stdout/stderr to ``10 000`` chars (the backend already does this,
    but we re-cap here so a misbehaving backend can't blow up the LLM context).

Output cap and timeout cap are dispatcher-level invariants — backends inherit
them but the user-visible JSON payload is shaped here.
"""

import json
import os
import re

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from core.plugins.sandbox import (
    SandboxBackend,
    SandboxResult,
    SandboxStartupError,
    make_sandbox_backend,
)
from utils.logger import logger

_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/(\s|$|\*)",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r":\(\)\s*\{.*?;.*?\}\s*;:",  # fork bomb
    r">\s*/dev/(sd|nvme|hd)\w*",  # raw-device overwrite
    r"\bchmod\s+-R\s+000\s+/",
]

_MAX_OUTPUT_CHARS = 10_000
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120

_SANDBOX_NETWORK = os.getenv("EXECUTE_CODE_NETWORK", "allow").lower()
_SANDBOX_MEMORY_MB = int(os.getenv("EXECUTE_CODE_MEMORY_MB", "512"))


def _network_allowed() -> bool:
    return _SANDBOX_NETWORK not in ("deny", "off", "false", "0", "no")


# Process-wide singleton — first call resolves the backend (Local vs Docker)
# and pings any external dependency (Docker daemon). Subsequent calls reuse it.
_BACKEND: SandboxBackend | None = None


def _get_backend() -> SandboxBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = make_sandbox_backend()
    return _BACKEND


def _validate_cwd(cwd: str | None) -> tuple[str, str | None]:
    """Validate user-supplied ``cwd``; return (normalized, error)."""
    if not cwd:
        return "", None
    if cwd.startswith("/") or cwd.startswith("\\"):
        return "", "cwd must be relative to the workspace (no leading /)"
    parts = [p for p in re.split(r"[\\/]+", cwd) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return "", "cwd cannot contain .. or escape the workspace"
    return "/".join(parts), None


class ExecuteCodeParams(BaseModel):
    code: str = Field(description="Code to execute: bash commands or Python")
    language: str = Field(default="bash", description="Execution language: bash or python (default bash)")
    cwd: str | None = Field(
        default=None,
        description="Optional workspace-relative subdirectory; defaults to the workspace root",
    )
    timeout: int = Field(
        default=_DEFAULT_TIMEOUT,
        description=f"Timeout in seconds (default {_DEFAULT_TIMEOUT}, maximum {_MAX_TIMEOUT})",
    )


@register_plugin(
    PluginMeta(
        name="execute_code",
        category="code",
        capabilities=["command-execution", "python-execution"],
        input_types=["text"],
        output_types=["json"],
        description="Execute bash or Python in the user workspace",
    )
)
class ExecuteCodeTool(BaseTool):
    name: str = "execute_code"
    description: str = (
        "Run bash or Python inside the current user workspace and return stdout/stderr. "
        "Use for scripts, file operations, data processing, validation and installing packages.\n"
        "- language: 'bash' (default) or 'python'\n"
        "- cwd: Optional subdirectory within the workspace; defaults to its root\n"
        "- timeout: Seconds, default 30, maximum 120; output above 10000 characters is truncated\n"
        "Paths must stay within the workspace. Obvious destructive commands such as rm -rf /, mkfs, shutdown and fork bombs are rejected."
    )
    param_anno = ExecuteCodeParams

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._user_id = kwargs.get("user_id") or "anonymous"
        self._session_id = kwargs.get("session_id") or "default"

    async def __call__(self, params: ExecuteCodeParams) -> str:
        code = (params.code or "").strip()
        if not code:
            return json.dumps({"success": False, "error": "code must not be empty"}, ensure_ascii=False)

        lang = (params.language or "bash").lower()
        if lang in ("sh", "shell"):
            lang = "bash"
        if lang not in ("bash", "python"):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Unsupported language: {lang} (use bash or python)",
                },
                ensure_ascii=False,
            )

        timeout = min(max(int(params.timeout or _DEFAULT_TIMEOUT), 1), _MAX_TIMEOUT)

        for pat in _DANGEROUS_PATTERNS:
            if re.search(pat, code, re.IGNORECASE):
                logger.warning(f"execute_code refused user={self._user_id} pattern={pat!r}")
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Execution rejected: blocked command pattern {pat!r}",
                    },
                    ensure_ascii=False,
                )

        rel_cwd, cwd_err = _validate_cwd(params.cwd)
        if cwd_err:
            return json.dumps({"success": False, "error": cwd_err}, ensure_ascii=False)

        try:
            backend = _get_backend()
        except SandboxStartupError as err:
            logger.error(f"execute_code backend startup failed: {err}")
            return json.dumps(
                {"success": False, "error": f"sandbox unavailable: {err}"},
                ensure_ascii=False,
            )

        logger.debug(
            f"execute_code user={self._user_id} session={self._session_id} lang={lang} cwd={rel_cwd or '.'} "
            f"timeout={timeout}s backend={type(backend).__name__}"
        )

        try:
            result: SandboxResult = await backend.run(
                user_id=self._user_id,
                session_id=self._session_id,
                cwd=rel_cwd,
                code=code,
                language=lang,
                timeout_s=timeout,
                memory_mb=_SANDBOX_MEMORY_MB,
                network=_network_allowed(),
            )
        except Exception as err:
            logger.error(f"execute_code failed user={self._user_id}: {err}")
            return json.dumps({"success": False, "error": str(err)}, ensure_ascii=False)

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        truncated = result.truncated
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS]
            truncated = True
        if len(stderr) > _MAX_OUTPUT_CHARS:
            stderr = stderr[:_MAX_OUTPUT_CHARS]
            truncated = True

        if result.timed_out:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Execution timed out (>{timeout}s)",
                    "sandbox": result.sandbox_label,
                    "stdout": stdout,
                },
                ensure_ascii=False,
            )

        payload: dict = {
            "success": result.return_code == 0,
            "language": lang,
            "cwd": rel_cwd or ".",
            "return_code": result.return_code,
            "stdout": stdout,
            "stderr": stderr,
            "sandbox": result.sandbox_label,
            "duration_ms": result.duration_ms,
        }
        if truncated:
            payload["note"] = f"Output exceeded {_MAX_OUTPUT_CHARS} characters and was truncated"
        return json.dumps(payload, ensure_ascii=False)
