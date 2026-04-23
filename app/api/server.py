"""EvoAgent FastAPI server."""

import logging
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routers import agent, auth, skills, sub_agent, user, workspace

# Suppress noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)


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


def main() -> None:
    """Entry point exposed via `evoagent-server` console script."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8194"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
