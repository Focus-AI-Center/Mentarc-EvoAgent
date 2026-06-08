"""EvoAgent FastAPI server."""

import logging
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routers import agent, auth, skills, sub_agent, user, workspace
from core.plugins.sandbox import make_sandbox_backend
from utils.logger import logger
from utils.storage_backend import get_backend

# Suppress noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)


# ── Startup config validation ────────────────────────────────────────────────
#
# Run at import time so a misconfigured deployment crashes the worker
# immediately rather than failing at first request. The two factories below
# also fail fast on cross-mode configurations (LocalFS storage + remote
# sandbox, or MinIO storage + local sandbox) — see plan §1.0.

def _validate_runtime_config() -> None:
    storage = get_backend()  # eager: creates the singleton, surfaces missing creds
    storage_label = type(storage).__name__
    minio_endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    mode = "MINIO" if minio_endpoint else "LOCAL"

    sandbox = make_sandbox_backend()
    # The factory wraps the chosen backend in MetricsSandboxBackend; unwrap so
    # the startup log shows the real backend (Local… / RemoteDocker…) and not
    # the decorator's class name.
    sandbox_inner = getattr(sandbox, "_inner", sandbox)
    sandbox_label = type(sandbox_inner).__name__

    extras: list[str] = []
    if mode == "MINIO":
        bucket = os.getenv("MINIO_BUCKET", "evoagent-workspaces")
        extras.append(f"bucket={bucket}")
        extras.append(f"endpoint={minio_endpoint}")
        extras.append(f"docker={os.getenv('DOCKER_HOST', '<local>')}")
        extras.append(
            f"image={os.getenv('SANDBOX_IMAGE', 'evoagent-sandbox:python3.11')}"
        )
    else:
        extras.append(f"workspace={os.getenv('WORKSPACE_ROOT', 'data/workspace')}")

    logger.info(
        f"runtime: mode={mode} storage={storage_label} sandbox={sandbox_label} "
        + " ".join(extras)
    )


_validate_runtime_config()


# -- optional rate limiting (slowapi) ----------------------------------------
# Enabled when RATE_LIMIT is set (e.g. "20/minute"). Falls back to a no-op
# dependency when slowapi isn't installed or RATE_LIMIT is empty.
_RATE_LIMIT = os.getenv("RATE_LIMIT", "").strip()

try:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    _limiter = Limiter(key_func=get_remote_address, default_limits=[_RATE_LIMIT] if _RATE_LIMIT else [])
except ImportError:  # pragma: no cover — slowapi is optional
    _limiter = None


app = FastAPI(title="EvoAgent", description="An evolvable LLM agent framework", version="1.0.0")

if _limiter is not None and _RATE_LIMIT:
    app.state.limiter = _limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            {"detail": f"Rate limit exceeded: {exc.detail}"},
            status_code=429,
            headers={"Retry-After": "60"},
        )


def _allowed_origins() -> list[str]:
    """Parse ALLOWED_ORIGINS env var; fall back to localhost-only defaults.

    Setting ``ALLOWED_ORIGINS="*"`` explicitly re-enables the open default, but
    with ``allow_credentials=False`` per CORS spec (browsers reject ``*`` +
    credentials). For production, list specific origins:

        ALLOWED_ORIGINS="https://example.com,https://demo.example.com"
    """
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:8194", "http://127.0.0.1:8194"]
    return [o.strip() for o in raw.split(",") if o.strip()]


_origins = _allowed_origins()
_allow_credentials = "*" not in _origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(skills.router, prefix="/api/skills", tags=["skills"])
app.include_router(user.router, prefix="/api/user", tags=["user"])
app.include_router(workspace.router, prefix="/api/workspace", tags=["workspace"])
app.include_router(sub_agent.router, prefix="/api/sub-agent", tags=["sub-agent"])

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def root():
    return FileResponse(
        "app/static/index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.on_event("shutdown")
async def _shutdown_mcp_sessions():
    """Close long-lived MCP exec sessions and shared sandbox containers."""
    try:
        from core.mcp import close_all_sandbox_stdio_sessions
        from core.plugins.sandbox.session_docker import close_all_session_sandboxes

        await close_all_sandbox_stdio_sessions()
        await close_all_session_sandboxes()
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning(f"MCP sandbox session cleanup failed: {exc}")


@app.get("/metrics")
async def metrics():
    """Prometheus exposition endpoint.

    Single-process registry: this serves the snapshot of the current uvicorn
    worker. If we move to gunicorn multi-worker, switch to
    ``MultiProcessCollector`` and ``PROMETHEUS_MULTIPROC_DIR``; the metric
    names and label sets stay the same so dashboards don't break.
    """
    from fastapi.responses import Response

    from utils.metrics import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def main() -> None:
    """Entry point exposed via `evoagent-server` console script."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8194"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
