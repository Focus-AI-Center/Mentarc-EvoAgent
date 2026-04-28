"""Remote sandbox: Docker daemon + s3fs-mounted MinIO bucket.

Each invocation spawns a one-shot ``evoagent-sandbox`` container on the
configured Docker daemon. The container's entrypoint:

  1. mounts the MinIO bucket at ``/workspace/{user_id}`` via s3fs (so the
     sandbox sees the same tree the server's ``StorageBackend`` writes to);
  2. ``cd``s into ``/workspace/{user_id}/{cwd}`` and executes the user's
     code via bash or python;
  3. ``fusermount -u`` and exits.

The sandbox runs with:

  - ``--cap-add SYS_ADMIN`` and ``--device /dev/fuse`` (s3fs requirement).
  - ``--security-opt apparmor=unconfined`` (fuse needs it under default
    apparmor profiles on Ubuntu/Debian; harmless on Alibaba Linux).
  - ``--rm`` so containers self-clean on exit.
  - Bucket-scoped credentials passed via env (M3 will replace these with
    STS tokens scoped to ``{user_id}/*`` with a 5-minute TTL; for now we
    pass the same creds the server uses).

This first version uses simple env-var creds (item §3.3.a in the plan).
M3 swaps them for STS AssumeRole.
"""

from __future__ import annotations

import asyncio
import os
import time

from utils.logger import logger

from . import SandboxResult, SandboxStartupError

_MAX_OUTPUT_CHARS = 10_000


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) > _MAX_OUTPUT_CHARS:
        return s[:_MAX_OUTPUT_CHARS], True
    return s, False


class RemoteDockerBackend:
    """``SandboxBackend`` that delegates to a Docker daemon.

    The ``docker`` SDK reads ``DOCKER_HOST`` from the environment, so a single
    env var switches between local socket / SSH / TCP transports. The first
    container start may take a few seconds (image pull, FUSE bring-up); subsequent
    calls reuse the cached image.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        minio_endpoint: str | None = None,
        minio_access_key: str | None = None,
        minio_secret_key: str | None = None,
        minio_bucket: str | None = None,
    ):
        self.image = image or os.getenv("SANDBOX_IMAGE", "evoagent-sandbox:python3.11")
        self.network = network or os.getenv("SANDBOX_NETWORK", "host")
        # Defer required-env validation until ``run()``: the factory may be
        # instantiated for inspection (tests, server startup logging) before
        # the full MinIO credential set has been put into place. Cross-mode
        # config is already rejected upstream in ``utils.storage_backend``.
        self.minio_endpoint = minio_endpoint or os.getenv("MINIO_ENDPOINT", "")
        self.minio_access_key = minio_access_key or os.getenv("MINIO_ACCESS_KEY", "")
        self.minio_secret_key = minio_secret_key or os.getenv("MINIO_SECRET_KEY", "")
        self.minio_bucket = minio_bucket or os.getenv(
            "MINIO_BUCKET", "evoagent-workspaces"
        )
        # Lazy: build client on first use so importing this module on a host
        # without Docker is fine (factory still rejects, but tests can import).
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import docker  # type: ignore
            except ImportError as err:
                raise SandboxStartupError(
                    "RemoteDockerBackend requires the `docker` Python package; "
                    "install with `pip install docker>=7`."
                ) from err
            try:
                self._client = docker.from_env()
                # Cheap health probe — surfaces "daemon not reachable" with a
                # clear error instead of failing on the first run().
                self._client.ping()
            except Exception as err:
                raise SandboxStartupError(
                    f"Cannot reach Docker daemon (DOCKER_HOST={os.getenv('DOCKER_HOST', '<local>')}): {err}"
                ) from err
        return self._client

    async def run(
        self,
        *,
        user_id: str,
        cwd: str,
        code: str,
        language: str,
        timeout_s: int,
        memory_mb: int,
        network: bool,  # accepted for API parity with LocalBwrapBackend
    ) -> SandboxResult:
        sandbox_label = f"docker({self.image},net={self.network})"
        if not self.minio_endpoint or not self.minio_access_key or not self.minio_secret_key:
            raise SandboxStartupError(
                "RemoteDockerBackend requires MINIO_ENDPOINT / MINIO_ACCESS_KEY / "
                "MINIO_SECRET_KEY to be set"
            )
        env = {
            "MINIO_ENDPOINT": self.minio_endpoint,
            "MINIO_ACCESS_KEY": self.minio_access_key,
            "MINIO_SECRET_KEY": self.minio_secret_key,
            "MINIO_BUCKET": self.minio_bucket,
            "USER_ID": user_id,
            "REL_CWD": cwd or "",
            "LANGUAGE": language,
            "TIMEOUT_S": str(timeout_s),
            "CODE": code,
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        }

        host_config = {
            "image": self.image,
            "environment": env,
            "network_mode": self.network,
            "cap_add": ["SYS_ADMIN"],
            "devices": ["/dev/fuse:/dev/fuse:rwm"],
            "security_opt": ["apparmor=unconfined"],
            "auto_remove": False,  # we read logs after exit, then remove
            "detach": True,
            "stdin_open": False,
            "tty": False,
            "mem_limit": f"{memory_mb}m" if memory_mb > 0 else None,
        }
        # Drop None so the SDK uses its default for unset keys.
        host_config = {k: v for k, v in host_config.items() if v is not None}

        client = self._get_client()
        t0 = time.time()
        container = None
        try:
            container = await asyncio.to_thread(
                client.containers.run, **host_config
            )
            wait_result = await asyncio.to_thread(
                container.wait, timeout=timeout_s + 30
            )
            return_code = int(wait_result.get("StatusCode", -1))
            stdout_bytes = await asyncio.to_thread(
                container.logs, stdout=True, stderr=False
            )
            stderr_bytes = await asyncio.to_thread(
                container.logs, stdout=False, stderr=True
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        except Exception as err:
            logger.error(f"sandbox.docker user={user_id} container failed: {err}")
            return SandboxResult(
                return_code=-1,
                stdout="",
                stderr=f"sandbox container failure: {err}",
                duration_ms=int((time.time() - t0) * 1000),
                sandbox_label=sandbox_label,
            )
        finally:
            if container is not None:
                try:
                    await asyncio.to_thread(
                        container.remove, force=True, v=True
                    )
                except Exception as err:
                    logger.warning(
                        f"sandbox.docker container cleanup failed for user={user_id}: {err}"
                    )

        out, t1 = _truncate(stdout)
        err_text, t2 = _truncate(stderr)
        return SandboxResult(
            return_code=return_code,
            stdout=out,
            stderr=err_text,
            duration_ms=int((time.time() - t0) * 1000),
            sandbox_label=sandbox_label,
            truncated=t1 or t2,
        )


__all__ = ["RemoteDockerBackend"]
