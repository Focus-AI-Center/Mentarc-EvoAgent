"""S3-compatible object store backend (MinIO).

Uses the synchronous ``minio`` SDK and trampolines through ``asyncio.to_thread``.
The SDK manages its own connection pool, so concurrent ``asyncio.gather(...)``
fans out to real network parallelism.

ETag handling: MinIO returns the MD5 hex (in quotes) for non-multipart objects.
We strip the quotes once at the boundary so the rest of the codebase sees
plain hex strings — same shape as ``LocalFSBackend``.
"""

from __future__ import annotations

import asyncio
import io

from . import IF_MATCH_ABSENT, ETagMismatchError, KeyNotFoundError, StorageError


def _strip_etag(etag: str | None) -> str | None:
    if etag is None:
        return None
    return etag.strip('"').strip("'")


def _split_endpoint(endpoint: str) -> tuple[str, bool]:
    """Return (host:port, secure)."""
    if endpoint.startswith("http://"):
        return endpoint[len("http://") :].rstrip("/"), False
    if endpoint.startswith("https://"):
        return endpoint[len("https://") :].rstrip("/"), True
    return endpoint.rstrip("/"), False


class MinIOSDKBackend:
    """``StorageBackend`` backed by MinIO via the official SDK."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str = "evoagent-workspaces",
        secure: bool | None = None,
        region: str | None = None,
    ):
        from minio import Minio  # local import: keeps LOCAL mode startup minio-free

        host, default_secure = _split_endpoint(endpoint)
        self._client = Minio(
            host,
            access_key=access_key,
            secret_key=secret_key,
            secure=default_secure if secure is None else secure,
            region=region,
        )
        self.bucket = bucket
        self.endpoint = endpoint

    # ── helpers (sync, run via to_thread) ────────────────────────────────

    def _is_no_such_key(self, err) -> bool:
        from minio.error import S3Error

        return isinstance(err, S3Error) and err.code in (
            "NoSuchKey",
            "NoSuchObject",
        )

    def _read_bytes_sync(self, key: str) -> bytes:
        from minio.error import S3Error

        try:
            resp = self._client.get_object(self.bucket, key)
        except S3Error as err:
            if self._is_no_such_key(err):
                raise KeyNotFoundError(key) from err
            raise StorageError(f"MinIO get_object failed for {key!r}: {err}") from err
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def _stat_etag(self, key: str) -> str | None:
        from minio.error import S3Error

        try:
            stat = self._client.stat_object(self.bucket, key)
        except S3Error as err:
            if self._is_no_such_key(err):
                return None
            raise
        return _strip_etag(stat.etag)

    def _write_bytes_sync(self, key: str, data: bytes, if_match: str | None) -> str:
        if if_match is not None:
            cur = self._stat_etag(key)
            if if_match == IF_MATCH_ABSENT:
                if cur is not None:
                    raise ETagMismatchError(key, IF_MATCH_ABSENT, cur)
            else:
                if cur != if_match:
                    raise ETagMismatchError(key, if_match, cur)
        result = self._client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type="application/octet-stream",
        )
        return _strip_etag(result.etag) or ""

    def _list_keys_sync(self, prefix: str) -> list[str]:
        objects = self._client.list_objects(self.bucket, prefix=prefix, recursive=True)
        out: list[str] = []
        for obj in objects:
            name = obj.object_name
            # Skip "directory marker" zero-byte objects ending with "/" — they
            # appear in MinIO when something (s3fs / mc) creates an explicit
            # prefix. Returning them would surprise callers expecting file keys.
            if name.endswith("/"):
                continue
            out.append(name)
        out.sort()
        return out

    def _delete_sync(self, key_or_prefix: str) -> int:
        from minio.deleteobjects import DeleteObject
        from minio.error import S3Error

        # Try as exact key first (the common case from a single ``delete(key)``).
        try:
            self._client.stat_object(self.bucket, key_or_prefix)
            self._client.remove_object(self.bucket, key_or_prefix)
            return 1
        except S3Error as err:
            if not self._is_no_such_key(err):
                raise

        # Fall through: treat as prefix (recursive delete).
        objs = list(self._client.list_objects(self.bucket, prefix=key_or_prefix, recursive=True))
        if not objs:
            return 0
        delete_iter = (DeleteObject(o.object_name) for o in objs)
        # remove_objects returns a generator of errors; iterate to actually
        # execute the delete and surface any failures.
        errors = list(self._client.remove_objects(self.bucket, delete_iter))
        if errors:
            raise StorageError(
                f"MinIO remove_objects had {len(errors)} errors under {key_or_prefix!r}: "
                f"{errors[:3]}{' ...' if len(errors) > 3 else ''}"
            )
        return len(objs)

    # ── sync StorageBackend interface ────────────────────────────────────
    # Used by ``utils/workspace.py`` (synchronous public API). Each call is one
    # blocking minio-py round trip — fine for the hot path (small files), and
    # async siblings below cover bulk fan-out via ``asyncio.gather``.

    def read_bytes_sync(self, key: str) -> bytes:
        return self._read_bytes_sync(key)

    def read_text_sync(self, key: str) -> str:
        return self._read_bytes_sync(key).decode("utf-8")

    def write_bytes_sync(self, key: str, data: bytes, *, if_match: str | None = None) -> str:
        return self._write_bytes_sync(key, data, if_match)

    def write_text_sync(self, key: str, content: str, *, if_match: str | None = None) -> str:
        return self._write_bytes_sync(key, content.encode("utf-8"), if_match)

    def list_keys_sync(self, prefix: str) -> list[str]:
        return self._list_keys_sync(prefix)

    def delete_sync(self, key_or_prefix: str) -> int:
        return self._delete_sync(key_or_prefix)

    def head_etag_sync(self, key: str) -> str | None:
        return self._stat_etag(key)

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
        return await asyncio.to_thread(self._stat_etag, key)


__all__ = ["MinIOSDKBackend"]
