"""Agent chat API router."""

import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.schema.agent import AgentRequest
from core.agent import EvoAgent, run_session_update_stream
from core.plugins.internal import _FRONTMATTER_RE, _PARAM_TO_FIELD, _parse_user_fields
from utils.extractors import INPUT_EXTRACTORS, OUTPUT_EXTRACTORS
from utils.logger import logger
from utils.ws_call import ws_call

router = APIRouter()


async def _create_workspace(user_id: str):
    """Create a local-filesystem workspace for the user."""
    from utils.workspace import Workspace

    return Workspace(user_id)


@router.post("/chat")
async def chat(request: AgentRequest, req: Request):
    """SSE streaming chat endpoint.

    Creates an EvoAgent instance, loads session context (history + memory),
    runs skill matching, and streams the response.
    """
    user_id = str(request.userId)
    session_id = str(request.sessionId)
    request_id = request.requestId or str(uuid.uuid4())[:8]
    user_input = request.data.input
    if request.data.images:
        user_input += "\n\n[User-uploaded images]\n" + "\n".join(request.data.images)

    logger.info(
        f"Chat request: user={user_id} session={session_id} "
        f"request_id={request_id} plugins={request.plugins} "
        f"input={user_input[:200]}..."
    )

    workspace = await _create_workspace(user_id)

    agent = EvoAgent(
        user_id=user_id,
        session_id=session_id,
        plugins=request.plugins,  # None → use DEFAULT_PLUGINS from settings
        system_prompt=request.prompt,
        model=request.modelType,
        workspace=workspace,
    )

    # Read user profile for input-side extraction
    _field_to_param = {v: k for k, v in _PARAM_TO_FIELD.items()}
    try:
        raw = await ws_call(agent.workspace, "read_prompt", "USER")
        body = _FRONTMATTER_RE.sub("", raw, count=1).strip() if raw else ""
        fields = _parse_user_fields(body)
        user_profile = {_field_to_param[cn]: val for cn, val in fields.items() if cn in _field_to_param}
    except Exception:
        user_profile = {}

    async def generate():
        full_response = ""
        try:
            async for event in agent.run_stream(user_input):
                event["requestId"] = request_id
                if event.get("code") == 2:
                    full_response += event.get("content", "")

                # After stream_start, emit input-side extraction (code 11)
                # Try all input extractors (not just matched_skill) because the
                # initial skill match may differ from what the agent actually uses.
                if event.get("code") == 3 and event.get("content") == "stream_start":
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    for _ext_fn in INPUT_EXTRACTORS.values():
                        repl = _ext_fn(user_input, user_profile)
                        if repl:
                            yield f"data: {json.dumps({'code': 11, 'content': json.dumps(repl, ensure_ascii=False), 'requestId': request_id}, ensure_ascii=False)}\n\n"
                    continue

                # Before stream_end, emit output-side extraction (code 11)
                # Try all output extractors (not just matched_skill) because the
                # agent may load additional skills mid-session via skill.
                if event.get("code") == 3 and event.get("content") == "stream_end":
                    if full_response:
                        for _ext_fn in OUTPUT_EXTRACTORS.values():
                            analysis = _ext_fn(full_response)
                            if analysis:
                                yield f"data: {json.dumps({'code': 11, 'content': json.dumps(analysis, ensure_ascii=False), 'requestId': request_id}, ensure_ascii=False)}\n\n"

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'code': 3, 'content': f'error: {str(e)}', 'requestId': request_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


class UpdateSessionRequest(BaseModel):
    userId: str
    sessionId: str


@router.post("/update-session")
async def update_session(request: UpdateSessionRequest):
    """SSE streaming endpoint to update user profile and memory from session."""
    logger.info(f"Update session: user={request.userId} session={request.sessionId}")

    # Create workspace matching current backend (MCP or local)
    workspace = await _create_workspace(request.userId)

    async def generate():
        try:
            async for event in run_session_update_stream(
                request.userId, request.sessionId, workspace=workspace
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"Update session error: {e}")
            yield f"data: {json.dumps({'code': 3, 'content': f'error: {str(e)}'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
