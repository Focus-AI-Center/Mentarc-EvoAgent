"""Thin awaitable wrapper over Workspace methods.

Historically this helper dispatched between sync (Workspace) and async
(MCPWorkspace) backends. In the paper release only the local filesystem
Workspace is supported, so this reduces to calling the method directly.
"""

from typing import Any


async def ws_call(workspace, method_name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(workspace, method_name)
    return method(*args, **kwargs)
