"""Sub-agent API router — independent endpoints for typed agents."""

import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schema.agent import AgentRequest
from core.sub_agent import SubAgentRunner, list_agent_types
from utils.logger import logger

router = APIRouter()


@router.get("/types")
async def get_agent_types():
    """List all available sub-agent types."""
    types = list_agent_types()
    return {
        "types": [{"name": t.name, "description": t.description, "skills": t.skills} for t in types],
        "count": len(types),
    }


@router.post("/{agent_type}/chat")
async def sub_agent_chat(agent_type: str, request: AgentRequest):
    """SSE streaming chat for a specific sub-agent type."""
    user_id = str(request.userId)
    session_id = str(request.sessionId)
    request_id = request.requestId or str(uuid.uuid4())[:8]
    user_input = request.data.input
    if request.data.images:
        user_input += "\n\n[用户上传的图片]\n" + "\n".join(request.data.images)

    logger.info(
        f"Sub-agent chat: type={agent_type} user={user_id} session={session_id} input={user_input[:200]}..."
    )

    # Create workspace
    from app.api.routers.agent import _create_workspace

    try:
        workspace = await _create_workspace(user_id)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e

    try:
        runner = SubAgentRunner(
            agent_type=agent_type,
            user_id=user_id,
            session_id=session_id,
            workspace=workspace,
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e

    async def generate():
        try:
            async for event in runner.run_stream(user_input):
                event["requestId"] = request_id
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"Sub-agent stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'code': 3, 'content': f'error: {str(e)}', 'requestId': request_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
