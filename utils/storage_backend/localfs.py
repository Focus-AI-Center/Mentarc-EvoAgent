"""POSIX filesystem backend rooted at ``WORKSPACE_ROOT``.

Keys map 1:1 to relative file paths (forward-slash separated). Writes go
through a ``.tmp`` file then ``Path.replace`` for atomic publish, matching
the existing behaviour of ``utils/workspace.py``'s helpers.

ETag = MD5 hex of the file's bytes. Cheap (small files dominate workspace
data — SOUL.md, MEMORY.md, JSON sessions). The cost is one extra read on
every write that uses ``if_match=...``; that's acceptable for the workloads
that actually need CAS (history_save, memory_update).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import shutil
import tempfile
from pathlib import Path

from configs.settings import WORKSPACE_ROOT

from . import IF_MATCH_ABSENT, ETagMismatchError, KeyNotFoundError


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class LocalFSBackend:
    """Filesystem-backed ``StorageBackend``.

    Async methods delegate to ``asyncio.to_thread`` so the event loop is not
    blocked on big files; per-op overhead is microseconds for the small files
    we actually store.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root if root is not None else WORKSPACE_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _resolve(self, key: str) -> Path:
        if not key:
            raise ValueError("Key must be non-empty")
        if key.startswith("/") or key.startswith("\\"):
            raise ValueError(f"Key must be relative: {key!r}")
        parts = key.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"Key contains empty / dot / parent segment: {key!r}")
        p = self.root.joinpath(*parts).resolve()
        try:
            p.relative_to(self.root)
        except ValueError as err:
            raise ValueError(f"Key escapes root: {key!r}") from err
        return p

    def _read_bytes_sync(self, key: str) -> bytes:
        p = self._resolve(key)
        try:
            return p.read_bytes()
        except FileNotFoundError as err:
            raise KeyNotFoundError(key) from err

    def _write_bytes_sync(self, key: str, data: bytes, if_match: str | None) -> str:
        p = self._resolve(key)
        # Stable locks outside the data tree work across backend instances and
        # processes, and never appear in workspace listings or snapshots.
        locks = self.root.parent / f".{self.root.name}.locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256(str(p).encode()).hexdigest()
        with (locks / lock_name).open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if if_match is not None:
                try:
                    cur = _md5_hex(p.read_bytes())
                except FileNotFoundError:
                    cur = None
                expected = None if if_match == IF_MATCH_ABSENT else if_match
                if cur != expected:
                    raise ETagMismatchError(key, if_match, cur)
            p.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".evoagent-write-", dir=p.parent, delete=False) as f:
                tmp = Path(f.name)
                try:
                    f.write(data)
                    f.close()
                    tmp.replace(p)
                finally:
                    tmp.unlink(missing_ok=True)
        return _md5_hex(data)

    def _list_keys_sync(self, prefix: str) -> list[str]:
        # Walk from the deepest existing directory implied by prefix to avoid
        # scanning the whole tree when caller is selective.
        if "\\" in prefix:
            prefix = prefix.replace("\\", "/")
        if "/" in prefix:
            head, _, _ = prefix.rpartition("/")
            walk_root = self.root / head if head else self.root
        else:
            walk_root = self.root
        try:
            walk_root_resolved = walk_root.resolve()
            walk_root_resolved.relative_to(self.root)
        except (FileNotFoundError, ValueError):
            return []
        if not walk_root_resolved.exists():
            return []
        results: list[str] = []
        for f in walk_root_resolved.rglob("*"):
            if not f.is_file() or f.name.startswith(".evoagent-write-"):
                continue
            rel = f.relative_to(self.root).as_posix()
            if rel.startswith(prefix):
                results.append(rel)
        results.sort()
        return results

    def _delete_sync(self, key_or_prefix: str) -> int:
        # Tolerate the conventional trailing-slash form for prefixes
        # (``"{user_id}/skills/foo/"``); ``_resolve`` would otherwise reject the
        # empty trailing segment. Doesn't change semantics for exact-key form.
        normalized = key_or_prefix.rstrip("/").rstrip("\\")
        if not normalized:
            return 0
        try:
            p = self._resolve(normalized)
        except ValueError:
            return 0
        if p.is_file():
            p.unlink()
            return 1
        if p.is_dir():
            count = sum(1 for f in p.rglob("*") if f.is_file())
            shutil.rmtree(p, ignore_errors=True)
            return count
        return 0

    def _head_etag_sync(self, key: str) -> str | None:
        try:
            p = self._resolve(key)
        except ValueError:
            return None
        try:
            return _md5_hex(p.read_bytes())
        except FileNotFoundError:
            return None

    # ── sync StorageBackend interface ────────────────────────────────────
    # Used by ``utils/workspace.py`` (synchronous public API). The async
    # counterparts below trampoline through these via ``asyncio.to_thread``.

    def read_bytes_sync(self, key: str) -> bytes:
        return self._read_bytes_sync(key)

    def read_text_sync(self, key: str) -> str:
        return self._read_bytes_sync(key).decode("utf-8")

    def read_text_with_etag_sync(self, key: str) -> tuple[str, str]:
        data = self._read_bytes_sync(key)
        return data.decode("utf-8"), _md5_hex(data)

    def write_bytes_sync(self, key: str, data: bytes, *, if_match: str | None = None) -> str:
        return self._write_bytes_sync(key, data, if_match)

    def write_text_sync(self, key: str, content: str, *, if_match: str | None = None) -> str:
        return self._write_bytes_sync(key, content.encode("utf-8"), if_match)

    def list_keys_sync(self, prefix: str) -> list[str]:
        return self._list_keys_sync(prefix)

    def delete_sync(self, key_or_prefix: str) -> int:
        return self._delete_sync(key_or_prefix)

    def head_etag_sync(self, key: str) -> str | None:
        return self._head_etag_sync(key)

    # ── async StorageBackend interface ───────────────────────────────────

    async def read_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._read_bytes_sync, key)

    async def read_text(self, key: str) -> str:
        data = await self.read_bytes(key)
        return data.decode("utf-8")

    async def write_bytes(self, key: str, data: bytes, *, if_match: str | None = None) -> str:
        return await asyncio.to_thread(self._write_bytes_sync, key, data, if_match)

    async def write_text(self, key: str, content: str, *, if_match: str | None = None) -> str:
        return await self.write_bytes(key, content.encode("utf-8"), if_match=if_match)

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_keys_sync, prefix)

    async def delete(self, key_or_prefix: str) -> int:
        return await asyncio.to_thread(self._delete_sync, key_or_prefix)

    async def head_etag(self, key: str) -> str | None:
        return await asyncio.to_thread(self._head_etag_sync, key)


__all__ = ["LocalFSBackend"]
