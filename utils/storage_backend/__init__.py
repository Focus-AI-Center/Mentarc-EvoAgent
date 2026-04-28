"""Pluggable storage backends for per-user workspace data.

Two implementations:

- ``LocalFSBackend`` — POSIX filesystem under ``WORKSPACE_ROOT`` (default,
  used when ``MINIO_ENDPOINT`` is unset). Atomic writes via ``tmp.replace``.
  ETag = MD5 of contents, computed on read/write to support optimistic CAS.

- ``MinIOSDKBackend`` — S3-compatible object store accessed via ``minio-py``.
  Selected automatically when ``MINIO_ENDPOINT`` is set. Native ETag from the
  object metadata. ``If-Match`` is implemented client-side: stat → compare →
  PUT. The race window is acceptable for single-user serial writes; multi-
  writer correctness will get tightened in M3 with `If-Match` request header
  if MinIO supports it server-side.

Both implementations share a common async interface (``StorageBackend``)
so callers can ``asyncio.gather(...)`` reads in parallel.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


class StorageError(Exception):
    """Base for all storage backend errors."""


class KeyNotFoundError(StorageError):
    """Raised when a key does not exist (read/head/delete on missing)."""

    def __init__(self, key: str):
        super().__init__(f"Key not found: {key!r}")
        self.key = key


class ETagMismatchError(StorageError):
    """Raised when ``write(if_match=...)`` fails the precondition.

    Callers (e.g. ``history_save``) should catch this, re-read, and retry —
    typically up to 3 times with small backoff before surfacing an error.
    """

    def __init__(self, key: str, expected: str | None, current: str | None):
        super().__init__(
            f"ETag mismatch on {key!r}: expected {expected!r}, current {current!r}"
        )
        self.key = key
        self.expected = expected
        self.current = current


# Sentinel for "must not exist" precondition (analog of S3 If-None-Match: *).
IF_MATCH_ABSENT = ""


@runtime_checkable
class StorageBackend(Protocol):
    """Async storage backend interface.

    Keys are forward-slash-separated, no leading slash, no ``..`` segments.
    The first segment is conventionally the user_id (e.g. ``alice/prompts/SOUL.md``);
    the backend itself does not enforce that — confinement is the caller's job.
    """

    async def read_text(self, key: str) -> str:
        """Return UTF-8 text. Raises ``KeyNotFoundError`` if missing."""

    async def write_text(
        self, key: str, content: str, *, if_match: str | None = None
    ) -> str:
        """Write UTF-8 text, return new ETag.

        ``if_match=None`` → no precondition.
        ``if_match=""``   → must not exist (create-only).
        ``if_match="<etag>"`` → must match current ETag exactly.
        Raises ``ETagMismatchError`` on precondition failure.
        """

    async def read_bytes(self, key: str) -> bytes:
        """Return raw bytes. Raises ``KeyNotFoundError`` if missing."""

    async def write_bytes(
        self, key: str, data: bytes, *, if_match: str | None = None
    ) -> str:
        """Write raw bytes, return new ETag. Same precondition semantics as write_text."""

    async def list_keys(self, prefix: str) -> list[str]:
        """List all keys whose name starts with ``prefix`` (recursive). Sorted."""

    async def delete(self, key_or_prefix: str) -> int:
        """Delete a single key or all keys under a prefix. Returns count deleted."""

    async def head_etag(self, key: str) -> str | None:
        """Return ETag if key exists, ``None`` otherwise."""


_BACKEND_SINGLETON: StorageBackend | None = None


def get_backend() -> StorageBackend:
    """Process-wide ``StorageBackend`` singleton.

    Lazy-initialised on first call (so importing this module on a host without
    MinIO config does not fail). Subsequent calls return the same instance —
    the SDK manages its own connection pool so a singleton is the right scope.
    """
    global _BACKEND_SINGLETON
    if _BACKEND_SINGLETON is None:
        _BACKEND_SINGLETON = make_backend()
    return _BACKEND_SINGLETON


def reset_backend() -> None:
    """Drop the cached singleton. For tests that need to re-read env vars."""
    global _BACKEND_SINGLETON
    _BACKEND_SINGLETON = None


def make_backend() -> StorageBackend:
    """Factory: pick the backend based on environment.

    - ``MINIO_ENDPOINT`` set → ``MinIOSDKBackend`` (also requires
      ``MINIO_ACCESS_KEY`` + ``MINIO_SECRET_KEY``; missing → ``RuntimeError``).
    - ``MINIO_ENDPOINT`` unset → ``LocalFSBackend`` rooted at ``WORKSPACE_ROOT``.
    """
    endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    if endpoint:
        access = os.getenv("MINIO_ACCESS_KEY", "").strip()
        secret = os.getenv("MINIO_SECRET_KEY", "").strip()
        if not access or not secret:
            raise RuntimeError(
                "MINIO_ENDPOINT is set but MINIO_ACCESS_KEY / MINIO_SECRET_KEY are missing — "
                "refusing to start to avoid silent fallback."
            )
        from .minio_sdk import MinIOSDKBackend

        return MinIOSDKBackend(
            endpoint=endpoint,
            access_key=access,
            secret_key=secret,
            bucket=os.getenv("MINIO_BUCKET", "evoagent-workspaces"),
        )
    from .localfs import LocalFSBackend

    return LocalFSBackend()


__all__ = [
    "ETagMismatchError",
    "IF_MATCH_ABSENT",
    "KeyNotFoundError",
    "StorageBackend",
    "StorageError",
    "get_backend",
    "make_backend",
    "reset_backend",
]
