"""Code execution plugin — runs bash or Python inside the user's workspace.

The tool always operates with CWD confined to `WORKSPACE_ROOT/{user_id}/`
(optionally a sub-path), so each user has an isolated scratch directory.
Basic safety checks reject a small set of obviously destructive patterns.

Sandbox modes (controlled by the ``EXECUTE_CODE_SANDBOX`` env var):

  auto        (default)  Use bubblewrap on Linux when the ``bwrap`` binary is
                         available; otherwise fall back to a plain subprocess.
  bubblewrap             Force bubblewrap; log a warning + fall back if
                         ``bwrap`` is missing.
  off                    Always use a plain subprocess (legacy behaviour).

When bwrap is active every invocation spawns a fresh ephemeral sandbox that:

  - rw-binds the user's workspace directory at the SAME absolute path so
    ``images/``, ``skills/``, ``sessions/``, ``prompts/`` etc. are visible and
    writeable to code, and changes are instantly visible to other plugins /
    the HTTP `/raw/` endpoint on the host;
  - ro-binds the server's Python installation (``sys.prefix``) + system
    directories (``/usr``, ``/bin``, ``/lib*``, ``/etc/ssl``...) so the full
    stdlib + installed site-packages + standard shell utilities are available;
  - unshares user / ipc / pid / uts / cgroup namespaces; network is shared by
    default (opt out with ``EXECUTE_CODE_NETWORK=deny``);
  - applies ``prlimit`` for memory (``EXECUTE_CODE_MEMORY_MB``, default 512)
    and CPU time (= ``timeout``).

Remaining hardening (future work, not yet implemented):

  1. Dedicated sandbox Python venv with an allow-list of packages, ro-bound
     instead of the server's own ``sys.prefix`` (prevents leakage of the
     repo's own code when deployed via ``pip install -e .``).
  2. Ephemeral container per invocation (gVisor / Kata / Firecracker) for
     VM-grade isolation.
  3. Remote sandbox service (E2B, Modal, Daytona, ...).
  4. seccomp-bpf syscall allow-list on top of bwrap.
  5. Network egress allow-list per skill.
  6. Per-user concurrency cap + global QPS cap.
  7. Audit log of ``{user_id, code, cwd, returncode, duration_ms}``.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from configs.settings import WORKSPACE_ROOT
from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
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


# --- Sandbox config (read once at import) ------------------------------------

_SANDBOX_MODE = os.getenv("EXECUTE_CODE_SANDBOX", "auto").lower()
_SANDBOX_NETWORK = os.getenv("EXECUTE_CODE_NETWORK", "allow").lower()
_SANDBOX_MEMORY_MB = int(os.getenv("EXECUTE_CODE_MEMORY_MB", "512"))


def _resolved_sandbox_mode() -> str:
    """Resolve the requested ``EXECUTE_CODE_SANDBOX`` value to ``off`` or ``bubblewrap``.

    - ``off`` / ``none`` / ``disabled`` / ``false`` / ``0`` → ``off``
    - ``bubblewrap`` → ``bubblewrap`` if ``bwrap`` binary is available and
      host is Linux; otherwise ``off`` (logs a warning once).
    - ``auto`` (default) → ``bubblewrap`` on Linux when ``bwrap`` exists,
      otherwise ``off`` silently.
    """
    if _SANDBOX_MODE in ("off", "none", "disabled", "false", "0"):
        return "off"
    has_bwrap = platform.system() == "Linux" and shutil.which("bwrap") is not None
    if _SANDBOX_MODE == "bubblewrap":
        if not has_bwrap:
            logger.warning(
                "EXECUTE_CODE_SANDBOX=bubblewrap requested but `bwrap` is not "
                "available on this host; falling back to unsandboxed subprocess."
            )
            return "off"
        return "bubblewrap"
    # auto
    return "bubblewrap" if has_bwrap else "off"


def _network_allowed() -> bool:
    return _SANDBOX_NETWORK not in ("deny", "off", "false", "0", "no")


def _user_workspace_root(cwd: Path) -> Path:
    """Return the per-user workspace directory containing ``cwd``.

    If ``cwd`` is already the user's workspace root (i.e. a direct child of
    ``WORKSPACE_ROOT``), return it. If ``cwd`` is a subdirectory, walk up to
    the user-level directory so bind-mount covers siblings like ``images/``.
    Falls back to ``cwd`` when the path is not under ``WORKSPACE_ROOT``
    (shouldn't happen given the earlier ``relative_to`` check in ``__call__``).
    """
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
    """Wrap ``cmd`` in ``prlimit + bwrap`` for workspace-compatible sandboxing.

    - Binds the user's per-user workspace directory at the SAME absolute path
      (rw), so bash/Python inside the sandbox see the exact same paths that
      the host uses — no translation layer needed by other plugins.
    - Ro-binds system dirs + the server's ``sys.prefix`` so stdlib /
      site-packages / shell utilities remain available.
    - Unshares user / ipc / pid / uts / cgroup namespaces. Network only if
      ``network`` is True.
    - ``prlimit`` applies ``RLIMIT_AS`` (memory) and ``RLIMIT_CPU``.
    """
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
    if network:
        args += ["--share-net"]
    else:
        args += ["--unshare-net"]

    # System (read-only, best-effort; --ro-bind-try silently skips missing paths)
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

    # Python installation — sys.prefix covers {bin,lib}/python/site-packages
    args += ["--ro-bind-try", sys.prefix, sys.prefix]
    if sys.base_prefix != sys.prefix:
        args += ["--ro-bind-try", sys.base_prefix, sys.base_prefix]

    # User workspace at same absolute path (rw)
    args += ["--bind", str(user_ws), str(user_ws)]

    # Minimal runtime filesystems
    args += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        str(cwd),
    ]

    # Env vars (explicit — bwrap does not inherit)
    for k, v in env.items():
        args += ["--setenv", k, str(v)]

    full = args + list(cmd)

    # Resource limits wrapper. prlimit applies before exec, inherited by children.
    if memory_mb > 0 or timeout > 0:
        limits = ["prlimit"]
        if memory_mb > 0:
            limits.append(f"--as={memory_mb * 1024 * 1024}")
        if timeout > 0:
            limits.append(f"--cpu={timeout}")
        full = limits + full

    return full


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

    def _workspace_dir(self) -> Path:
        return Path(WORKSPACE_ROOT).resolve() / self._user_id

    async def __call__(self, params: ExecuteCodeParams) -> str:
        code = (params.code or "").strip()
        if not code:
            return json.dumps({"success": False, "error": "code 不能为空"}, ensure_ascii=False)

        lang = (params.language or "bash").lower()
        timeout = min(max(int(params.timeout or _DEFAULT_TIMEOUT), 1), _MAX_TIMEOUT)

        # Safety: reject obvious destructive patterns
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

        # Resolve cwd strictly within the user's workspace
        ws_dir = self._workspace_dir()
        ws_dir.mkdir(parents=True, exist_ok=True)
        if params.cwd:
            try:
                cwd_path = (ws_dir / params.cwd).resolve()
                cwd_path.relative_to(ws_dir)
            except (ValueError, OSError) as e:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"cwd 必须位于用户 workspace 内: {e}",
                    },
                    ensure_ascii=False,
                )
            cwd_path.mkdir(parents=True, exist_ok=True)
        else:
            cwd_path = ws_dir

        # Build the subprocess command
        if lang == "python":
            cmd = [sys.executable, "-c", code]
        elif lang in ("bash", "sh", "shell"):
            cmd = ["bash", "-c", code]
        else:
            return json.dumps(
                {
                    "success": False,
                    "error": f"不支持的 language: {lang}（仅支持 bash / python）",
                },
                ensure_ascii=False,
            )

        # PATH also prepends sys.prefix/bin so plain `python` / `pip` without
        # absolute paths resolve to the server's interpreter inside the sandbox.
        system_path = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env = {
            "HOME": str(cwd_path),
            "PATH": f"{sys.prefix}/bin:{system_path}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }

        mode = _resolved_sandbox_mode()
        if mode == "bubblewrap":
            full_cmd = _build_bwrap_command(
                cmd,
                cwd_path,
                env,
                timeout=timeout,
                network=_network_allowed(),
                memory_mb=_SANDBOX_MEMORY_MB,
            )
            # bwrap owns cwd + env; do not double-set on subprocess.
            run_kwargs: dict = {}
            sandbox_label = (
                f"bubblewrap(network={'on' if _network_allowed() else 'off'},mem={_SANDBOX_MEMORY_MB}MB)"
            )
        else:
            full_cmd = cmd
            run_kwargs = {"cwd": str(cwd_path), "env": env}
            sandbox_label = "none"

        logger.debug(f"execute_code user={self._user_id} lang={lang} sandbox={sandbox_label} cwd={cwd_path}")

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                # Small grace period so prlimit CPU can kill first on CPU-bound bombs.
                timeout=timeout + 5,
                **run_kwargs,
            )
        except subprocess.TimeoutExpired:
            return json.dumps(
                {
                    "success": False,
                    "error": f"执行超时（>{timeout}s）",
                    "sandbox": sandbox_label,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"execute_code failed user={self._user_id}: {e}")
            return json.dumps(
                {"success": False, "error": str(e), "sandbox": sandbox_label},
                ensure_ascii=False,
            )

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        truncated = False
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS]
            truncated = True
        if len(stderr) > _MAX_OUTPUT_CHARS:
            stderr = stderr[:_MAX_OUTPUT_CHARS]
            truncated = True

        try:
            rel_cwd = str(cwd_path.relative_to(ws_dir)) or "."
        except ValueError:
            rel_cwd = str(cwd_path)

        payload = {
            "success": result.returncode == 0,
            "language": lang,
            "cwd": rel_cwd,
            "return_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "sandbox": sandbox_label,
        }
        if truncated:
            payload["note"] = f"输出超过 {_MAX_OUTPUT_CHARS} 字符，已截断"
        return json.dumps(payload, ensure_ascii=False)
