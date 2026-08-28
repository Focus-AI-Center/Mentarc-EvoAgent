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
        return "", "cwd 必须是 workspace 下的相对路径（不能以 / 开头）"
    parts = [p for p in re.split(r"[\\/]+", cwd) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return "", "cwd 不能包含 .. （禁止跳出 workspace）"
    return "/".join(parts), None


class ExecuteCodeParams(BaseModel):
    code: str = Field(description="要执行的代码（bash 命令或 Python 代码）")
    language: str = Field(default="bash", description="执行语言: bash 或 python（默认 bash）")
    cwd: str | None = Field(
        default=None,
        description="（可选）用户 workspace 下的子目录；留空则在 workspace 根目录执行",
    )
    timeout: int = Field(
        default=_DEFAULT_TIMEOUT,
        description=f"超时秒数（默认 {_DEFAULT_TIMEOUT}，最大 {_MAX_TIMEOUT}）",
    )


@register_plugin(
    PluginMeta(
        name="execute_code",
        category="code",
        capabilities=["command-execution", "python-execution"],
        input_types=["text"],
        output_types=["json"],
        description="在用户 workspace 目录内执行 bash 或 Python 代码",
    )
)
class ExecuteCodeTool(BaseTool):
    name: str = "execute_code"
    description: str = (
        "在当前用户的 workspace 目录内执行 bash 命令或 Python 代码并返回 stdout/stderr。"
        "适合的场景：运行脚本、查看/创建文件、跑数据处理、验证结果、安装 pip 包等。\n"
        "- language: 'bash'（默认）或 'python'\n"
        "- cwd: 可选的 workspace 内子目录，不填则在 workspace 根目录\n"
        "- timeout: 秒，默认 30，上限 120；输出超过 10000 字符会被截断\n"
        "不会执行 workspace 之外的路径，拒绝明显的危险命令（rm -rf /、mkfs、shutdown、fork bomb 等）。"
    )
    param_anno = ExecuteCodeParams

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._user_id = kwargs.get("user_id") or "anonymous"
        self._session_id = kwargs.get("session_id") or "default"

    async def __call__(self, params: ExecuteCodeParams) -> str:
        code = (params.code or "").strip()
        if not code:
            return json.dumps({"success": False, "error": "code 不能为空"}, ensure_ascii=False)

        lang = (params.language or "bash").lower()
        if lang in ("sh", "shell"):
            lang = "bash"
        if lang not in ("bash", "python"):
            return json.dumps(
                {
                    "success": False,
                    "error": f"不支持的 language: {lang}（仅支持 bash / python）",
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
                        "error": f"拒绝执行：命中危险模式 {pat!r}",
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
                    "error": f"执行超时（>{timeout}s）",
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
            payload["note"] = f"输出超过 {_MAX_OUTPUT_CHARS} 字符，已截断"
        return json.dumps(payload, ensure_ascii=False)
