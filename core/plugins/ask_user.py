"""AskUserQuestion tool — structured option cards for user interaction.

When the agent needs the user to make a choice before continuing, it calls
this tool with a question and a list of options. The tool returns a JSON
marker that agent.py detects and emits as SSE code 10. The frontend renders
clickable option cards; the user's selection is sent as a new message.
"""

import json

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool


class OptionItem(BaseModel):
    label: str = Field(description="选项标题")
    description: str | None = Field(default=None, description="选项说明")


class AskUserQuestionParams(BaseModel):
    question: str = Field(description="要问用户的问题")
    options: list[OptionItem] = Field(description="选项列表，每项包含 label 和可选的 description")


class AskUserQuestionTool(BaseTool):
    name: str = "ask_user_question"
    description: str = (
        "向用户展示选项卡片，等待用户选择。\n"
        "用于任务执行中需要用户做选择才能继续的场景。\n"
        "调用后本轮结束，等待用户选择后再继续。"
    )
    param_anno = AskUserQuestionParams

    async def __call__(self, params: AskUserQuestionParams) -> str:
        result = {
            "type": "ask_user",
            "question": params.question,
            "options": [{"label": o.label, "description": o.description} for o in params.options],
        }
        return json.dumps(result, ensure_ascii=False)
