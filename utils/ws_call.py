"""Thin awaitable wrapper over Workspace methods.

Historically this helper dispatched between sync (Workspace) and async
(MCPWorkspace) backends. In the paper release only the local filesystem
Workspace is supported, so this reduces to running the sync call on the
default thread-pool — without that step the per-turn workspace I/O (MinIO
GETs in MINIO mode, sync filesystem reads in LOCAL mode) blocks the
asyncio event loop and prevents other coroutines from making progress.
"""

import asyncio
from typing import Any


async def ws_call(workspace, method_name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(workspace, method_name)
    return await asyncio.to_thread(method, *args, **kwargs)
