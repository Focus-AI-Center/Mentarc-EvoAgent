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
    label: str = Field(description="Option label")
    description: str | None = Field(default=None, description="Option description")


class AskUserQuestionParams(BaseModel):
    question: str = Field(description="Question for the user")
    options: list[OptionItem] = Field(description="Options with a label and optional description")


class AskUserQuestionTool(BaseTool):
    name: str = "ask_user_question"
    description: str = (
        "Present option cards and wait for the user to choose.\n"
        "Use when a user choice is needed to continue.\n"
        "The current turn ends after the call; resume when the user responds."
    )
    param_anno = AskUserQuestionParams

    async def __call__(self, params: AskUserQuestionParams) -> str:
        result = {
            "type": "ask_user",
            "question": params.question,
            "options": [{"label": o.label, "description": o.description} for o in params.options],
        }
        return json.dumps(result, ensure_ascii=False)
