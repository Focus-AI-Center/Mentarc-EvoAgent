"""Request/response models for Agent API."""

from pydantic import BaseModel, Field

from core.delegation import TASK_ID_PATTERN


class UserInput(BaseModel):
    input: str
    images: list[str] = []
    files: list[dict] = []


class MCPService(BaseModel):
    name: str
    url: str
    headers: dict[str, str] = {}


class AgentRequest(BaseModel):
    userId: str | int
    sessionId: str | int
    requestId: str | None = None
    taskId: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    data: UserInput
    plugins: list[str] | None = None
    prompt: str | None = None
    knowledgeBaseIds: list[str] = []
    mcpServices: list[MCPService] = []
    modelType: str | None = None  # "default" | "self-deployed"


class AgentResponse(BaseModel):
    code: int  # 2=text, 3=markers, 4=tool_call, 5=tool_result
    content: str
    metadata: dict = {}
