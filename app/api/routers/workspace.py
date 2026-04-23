"""Workspace API router — workspace file management."""

import base64

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from utils.image_upload import upload_image
from utils.workspace import Workspace

router = APIRouter()

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB


class FileUpdateRequest(BaseModel):
    content: str


class ClearRequest(BaseModel):
    """Request to clear user data."""

    confirm: bool = False
    reset_to_defaults: bool = False


class SnapshotRequest(BaseModel):
    snapshot: dict
    replace: bool = True


@router.get("/{user_id}/files")
async def list_files(user_id: str):
    """List all files in the user's workspace, grouped by directory."""
    ws = Workspace(user_id)

    # Prompts
    prompts = []
    for name in ws.list_prompts():
        content = ws.read_prompt(name)
        prompts.append({"name": f"{name}.md", "size": len(content.encode()), "content": content})

    # Skills
    skills = []
    for name, content in ws.skill_get_all().items():
        skills.append({"name": name, "size": len(content.encode()), "content": content})

    return {
        "dirs": {
            "prompts": prompts,
            "skills": skills,
            "sessions": [],
        }
    }


@router.get("/{user_id}/files/{subdir}/{filename:path}")
async def get_file(user_id: str, subdir: str, filename: str):
    """Read a single file from the workspace."""
    ws = Workspace(user_id)
    content = _read_file(ws, subdir, filename)
    if content is None:
        raise HTTPException(404, f"File '{subdir}/{filename}' not found")
    return {"name": filename, "subdir": subdir, "content": content}


@router.put("/{user_id}/files/{subdir}/{filename:path}")
async def update_file(user_id: str, subdir: str, filename: str, request: FileUpdateRequest):
    """Update a file in the workspace (only existing files)."""
    ws = Workspace(user_id)
    if _read_file(ws, subdir, filename) is None:
        raise HTTPException(404, f"File '{subdir}/{filename}' not found")
    _write_file(ws, subdir, filename, request.content)
    return {"status": "updated", "path": f"{subdir}/{filename}"}


def _read_file(ws: Workspace, subdir: str, filename: str) -> str | None:
    """Route a file read to the appropriate workspace method."""
    if subdir == "prompts":
        name = filename.removesuffix(".md")
        content = ws.read_prompt(name)
        return content if content else None
    if subdir == "skills":
        # filename is skill name (directory name) or "name/SKILL.md"
        skill_name = filename.split("/")[0] if "/" in filename else filename
        return ws.skill_get(skill_name)
    return None


def _write_file(ws: Workspace, subdir: str, filename: str, content: str) -> None:
    """Route a file write to the appropriate workspace method."""
    if subdir == "prompts":
        name = filename.removesuffix(".md")
        ws.write_prompt(name, content)
    elif subdir == "skills":
        skill_name = filename.split("/")[0] if "/" in filename else filename
        ws.skill_set(skill_name, content)


@router.get("/{user_id}/sessions")
async def list_sessions(user_id: str):
    """List all sessions with metadata (id, timestamp, preview)."""
    ws = Workspace(user_id)
    return {"sessions": ws.list_sessions()}


@router.get("/{user_id}/sessions/{session_id}")
async def get_session(user_id: str, session_id: str):
    """Get full conversation for a session."""
    ws = Workspace(user_id)
    data = ws.history_load(session_id)
    if not data:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return data


@router.get("/{user_id}/snapshot")
async def get_snapshot(user_id: str):
    """Export workspace snapshot for bridge restore."""
    ws = Workspace(user_id)
    return {"snapshot": ws.export_snapshot()}


@router.put("/{user_id}/snapshot")
async def put_snapshot(user_id: str, request: SnapshotRequest):
    """Import workspace snapshot for bridge bootstrap."""
    ws = Workspace(user_id)
    ws.import_snapshot(request.snapshot, replace=request.replace)
    return {"status": "ok", "user_id": user_id}


@router.post("/{user_id}/images/upload")
async def upload_images(user_id: str, files: list[UploadFile] = File(...)):
    """Upload one or more images to workspace.

    Accepts multipart/form-data with multiple image files.
    Each image is saved to the user's workspace and returns a public URL.
    """
    results = []
    ws = Workspace(user_id)

    for file in files:
        if file.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(400, f"不支持的图片格式: {file.content_type}，支持: png/jpg/webp")

        data = await file.read()
        if len(data) > MAX_IMAGE_SIZE:
            raise HTTPException(400, f"图片过大: {file.filename}（上限20MB）")

        try:
            b64_data = base64.b64encode(data).decode("utf-8")
            file_url, file_name = upload_image(b64_data, file.content_type)
            ws.image_write(file_name, data)
        except Exception as e:
            raise HTTPException(500, f"图片上传失败: {file.filename} — {e}") from e

        results.append(
            {
                "filename": file_name,
                "originalName": file.filename,
                "size": len(data),
                "url": file_url,
            }
        )

    return {"uploaded": results, "count": len(results)}


@router.get("/{user_id}/raw/{file_path:path}")
async def get_raw_file(user_id: str, file_path: str):
    """Serve raw binary files from workspace (e.g., images).

    Supports:
    - images/{filename} — Serve images from workspace storage image storage

    Args:
        user_id: User identifier
        file_path: Path to file (e.g., "images/abc123.png")

    Returns:
        Binary file content with appropriate Content-Type

    Raises:
        HTTPException 400: Invalid path (security check)
        HTTPException 404: File not found
    """
    # Security: prevent path traversal
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(400, "Invalid file path")

    ws = Workspace(user_id)

    # Route to appropriate handler based on path prefix
    if file_path.startswith("images/"):
        filename = file_path.removeprefix("images/")
        data = ws.image_read(filename)
        if data is None:
            raise HTTPException(404, f"Image '{filename}' not found")

        # Determine MIME type from extension
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/png")

        return Response(content=data, media_type=mime_type)

    if file_path.startswith("previews/"):
        preview_id = file_path.removeprefix("previews/")
        html = ws.preview_read(preview_id)
        if html is None:
            raise HTTPException(404, f"Preview '{preview_id}' not found")
        return Response(content=html, media_type="text/html; charset=utf-8")

    raise HTTPException(404, f"File path '{file_path}' not supported")


@router.get("/{user_id}/sync-skills")
async def sync_builtin_skills(user_id: str):
    """Sync builtin skills from configs/skills/ into user workspace.

    Updates existing builtin skills with latest content and adds new ones.
    User-created skills are preserved.
    """
    ws = Workspace(user_id)
    result = ws.sync_builtin_skills()
    return {
        "status": "ok",
        "added": result["added"],
        "updated": result["updated"],
        "message": f"技能同步完成：新增 {result['added']} 个，更新 {result['updated']} 个",
    }


@router.delete("/{user_id}")
async def clear_workspace(user_id: str, request: ClearRequest):
    """Clear all user data in workspace storage workspace.

    By default, clears sessions and user skills.
    Prompts (USER.md, MEMORY.md, etc.) are preserved.

    If reset_to_defaults=true, workspace is reset to initial state
    with default skills from configs/skills/.

    Args:
        user_id: User identifier
        request: ClearRequest with confirm flag and reset option

    Returns:
        Status and counts of cleared items

    Raises:
        HTTPException 400: If confirm is not true
    """
    if not request.confirm:
        raise HTTPException(status_code=400, detail="必须设置 confirm=true 才能清空数据")

    ws = Workspace(user_id)

    if request.reset_to_defaults:
        # Full reset: clear all + re-initialize with defaults
        cleared = ws.reset_to_defaults()
        return {
            "status": "ok",
            "action": "reset_to_defaults",
            "cleared": cleared,
            "message": "工作空间已重置到初始状态，默认技能已恢复",
        }
    else:
        # Partial clear: only clear user data, keep prompts
        cleared = ws.clear_all()
        return {
            "status": "ok",
            "action": "clear_all",
            "cleared": cleared,
            "message": "用户数据已清空（对话历史、已确认技能），prompts 保留",
        }


@router.post("/{user_id}/runtime-cleanup")
async def runtime_cleanup(user_id: str):
    """Delete only the current remote runtime workspace copy."""
    ws = Workspace(user_id)
    cleared = ws.delete_workspace()
    return {"status": "ok", "user_id": user_id, "cleared": cleared}
