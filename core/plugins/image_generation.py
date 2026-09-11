"""Image generation plugin — generates images from text prompts via Doubao API."""

import asyncio
import base64
import json
import os

import httpx
from pydantic import BaseModel, Field

from configs.settings import DOUBAO_API_KEY, DOUBAO_MODEL
from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from utils.image_upload import upload_image
from utils.logger import logger

# Doubao API (火山引擎 / OpenAI-compatible)
DOUBAO_API_BASE = os.getenv("DOUBAO_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
_MAX_RETRIES = 2
_TIMEOUT = 300.0

_RATIO_TO_SIZE = {
    "1:1": "2048x2048",
    "4:3": "2304x1728",
    "3:4": "1728x2304",
    "16:9": "2560x1440",
    "9:16": "1440x2560",
    "3:2": "2496x1664",
    "2:3": "1664x2496",
    "4:5": "1824x2280",
    "5:4": "2280x1824",
}


def _aspect_ratio_to_size(aspect_ratio: str) -> str:
    if aspect_ratio in _RATIO_TO_SIZE:
        return _RATIO_TO_SIZE[aspect_ratio]
    return "2048x2048"


class ImageGenerationParams(BaseModel):
    prompt: str = Field(description="英文图片描述提示词")
    aspect_ratio: str = Field(default="16:9", description="宽高比 (16:9, 4:3, 1:1, 9:16, etc.)")


@register_plugin(
    PluginMeta(
        name="image_generation",
        category="generation",
        capabilities=["text-to-image"],
        input_types=["text"],
        output_types=["image_url"],
        description="Generate images from text prompts via Doubao API",
    )
)
class ImageGenerationTool(BaseTool):
    name: str = "image_generation"
    description: str = "根据英文提示词生成图片。提示词建议：【艺术风格】+【主体描述】+【文字排版】。"
    param_anno = ImageGenerationParams
    user_id: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.user_id = kwargs.get("user_id", "")

    async def __call__(self, params: ImageGenerationParams) -> str:
        if not DOUBAO_API_KEY:
            return "Error: DOUBAO_API_KEY not configured."
        if not self.user_id:
            return "Error: user_id required."

        url = os.getenv("DOUBAO_GENERATIONS_URL") or f"{DOUBAO_API_BASE}/images/generations"
        size = _aspect_ratio_to_size(params.aspect_ratio)
        body = {
            "model": DOUBAO_MODEL,
            "prompt": params.prompt,
            "response_format": "b64_json",
            "size": size,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DOUBAO_API_KEY}",
        }

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, json=body)

                if resp.status_code != 200:
                    if resp.status_code in {502, 503, 504} and attempt < _MAX_RETRIES:
                        await asyncio.sleep(2**attempt)
                        continue
                    return f"Image generation failed: HTTP {resp.status_code}"

                data_list = resp.json().get("data", [])
                images_b64 = [item["b64_json"] for item in data_list if item.get("b64_json")]
                if not images_b64:
                    return "No image data in response"

                img_bytes = base64.b64decode(images_b64[0])
                img_url = upload_image(img_bytes, ext="png", user_id=self.user_id)
                return json.dumps(
                    {
                        "image_path": img_url,
                        "prompt": params.prompt,
                        "aspect_ratio": params.aspect_ratio,
                        "markdown": f"![生成图片]({img_url})",
                    },
                    ensure_ascii=False,
                )

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
                return f"Image generation timeout ({_TIMEOUT}s)"
            except Exception as e:
                logger.error(f"Image generation error: {e}")
                return f"Image generation failed: {e}"

        return "Image generation failed: all retries exhausted"
