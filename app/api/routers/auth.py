"""Authentication API router — simple file-based register/login.

.. warning::

    **Demo-grade authentication only.** This module backs a single-tenant
    research deployment. Passwords are hashed with SHA-256 (not bcrypt /
    argon2), the hash file is plain JSON on local disk, and there are no
    sessions / JWTs / rate limits. **Do not expose this endpoint on the
    public internet.** Replace with a real auth provider (OAuth, SSO, or at
    minimum a bcrypt + JWT stack) before any production use.
"""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

_USERS_FILE = Path("data/users.json")

# Salt comes from env in deployment; the hardcoded fallback is explicitly a
# demo value and emits a warning once at import time.
_DEFAULT_SALT = "evoagent_demo_salt_2026"
_SALT = os.getenv("AUTH_SALT") or _DEFAULT_SALT
if _SALT == _DEFAULT_SALT:
    logger.warning(
        "auth.py is using the built-in demo salt. Set AUTH_SALT in the "
        "environment before any non-local deployment."
    )


class AuthRequest(BaseModel):
    username: str
    password: str


def _hash_password(password: str) -> str:
    return hashlib.sha256((_SALT + password).encode()).hexdigest()


def _load_users() -> dict:
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_users(users: dict) -> None:
    _USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _USERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _USERS_FILE)


@router.post("/register")
async def register(req: AuthRequest):
    username = req.username.strip()
    if not username or len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符 / Username must be at least 2 characters")
    if len(username) > 30:
        raise HTTPException(400, "用户名不能超过 30 个字符 / Username must be at most 30 characters")
    if not req.password or len(req.password) < 3:
        raise HTTPException(400, "密码至少 3 个字符 / Password must be at least 3 characters")

    users = _load_users()
    if username in users:
        raise HTTPException(400, "用户名已存在 / Username already exists")

    users[username] = {
        "password_hash": _hash_password(req.password),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _save_users(users)
    return {"status": "ok", "username": username}


@router.post("/login")
async def login(req: AuthRequest):
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(400, "请输入用户名和密码 / Username and password required")

    users = _load_users()
    user = users.get(username)
    if not user or user["password_hash"] != _hash_password(req.password):
        raise HTTPException(401, "用户名或密码错误 / Invalid username or password")

    return {"status": "ok", "username": username}
