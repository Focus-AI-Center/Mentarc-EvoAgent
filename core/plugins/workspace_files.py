"""Text artifact access through Workspace, without a sandbox round trip."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from utils.ws_call import ws_call

MAX_FILE_BYTES = 1024 * 1024
READ_CHARS = 20000


class WorkspaceFileParams(BaseModel):
    action: Literal["read", "write", "list"]
    path: str = Field(min_length=1, max_length=1024, description="Path relative to the user workspace")
    content: str | None = Field(default=None, description="Complete UTF-8 text, required for write")


class WorkspaceReadParams(WorkspaceFileParams):
    action: Literal["read", "list"]


class WorkspaceFileTool(BaseTool):
    name = "workspace_file"
    description = (
        "Read or list workspace text files directly through storage. Use this to inspect worker artifacts. "
        "A worker can also write UTF-8 text in its assigned output directory. "
        "Read returns up to 20,000 characters; use execute_code for larger selections or binary files."
    )
    param_anno = WorkspaceFileParams

    def __init__(self, workspace, *, output_dir=None):
        self.workspace, self.output_dir = workspace, output_dir
        if output_dir is None:
            self.param_anno = WorkspaceReadParams

    async def __call__(self, params: WorkspaceFileParams) -> str:
        # Use the same traversal and local symlink validation as all Workspace file access.
        self.workspace._workspace_file_key(params.path)
        if params.action == "read":
            text = await ws_call(self.workspace, "file_read", params.path)
            if text is None:
                return json.dumps({"status": "error", "error": "File not found.", "path": params.path})
            payload = {"path": params.path, "content": text[:READ_CHARS], "truncated": len(text) > READ_CHARS}
        elif params.action == "list":
            paths = await ws_call(self.workspace, "file_list", params.path)
            payload = {"files": paths[:100], "truncated": len(paths) > 100}
        else:
            if self.output_dir is None or not params.path.startswith(self.output_dir + "/"):
                return json.dumps(
                    {"status": "error", "error": "Writes require the assigned worker output directory."}
                )
            if params.path == self.output_dir + "/response.md":
                return json.dumps({"status": "error", "error": "response.md is reserved for the runtime."})
            if params.content is None or len(params.content.encode("utf-8")) > MAX_FILE_BYTES:
                return json.dumps({"status": "error", "error": "Write requires text of at most 1 MiB."})
            await ws_call(self.workspace, "file_write", params.path, params.content)
            payload = {"path": params.path, "bytes": len(params.content.encode("utf-8"))}
        return json.dumps(payload, ensure_ascii=False)
