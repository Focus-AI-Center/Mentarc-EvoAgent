"""Image understanding plugin - describes images from URLs using a vision model.

Downloads the image, converts to base64, and sends to a vision-capable LLM
for detailed description.
"""

import base64

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from configs.settings import (
    MODEL_VISION,
    OPENAI_API_BASE_VISION,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from utils.logger import logger


class ImageUnderstandingParams(BaseModel):
    url: str = Field(description="Image URL to analyze")
    question: str = Field(
        default="Describe the contents of this image in detail.",
        description="Specific question about the image (default: general description)",
    )


@register_plugin(
    PluginMeta(
        name="image_understanding",
        category="analysis",
        capabilities=["image_analysis", "image_description", "ocr"],
        input_types=["image_url"],
        output_types=["text"],
        description="Analyze and describe images from URLs",
    )
)
class ImageUnderstandingTool(BaseTool):
    name: str = "image_understanding"
    description: str = (
        "Analyze an image from a URL. Downloads the image from the given URL "
        "and uses a vision model to describe its content or answer questions about it. "
        "Supports jpg, png, gif, webp formats. Only accepts HTTP/HTTPS URLs."
    )
    param_anno = ImageUnderstandingParams

    async def __call__(self, params: ImageUnderstandingParams) -> str:
        p = params

        # Download image from URL and convert to base64
        try:
            async with httpx.AsyncClient(
                timeout=30,
                proxy=PROXY_SERVER if PROXY_SERVER else None,
            ) as http:
                resp = await http.get(p.url)
                resp.raise_for_status()
        except Exception as e:
            logger.error(f"Image download failed: {e}")
            return f"Image download failed: {str(e)}"

        content_type = resp.headers.get("content-type", "image/png")
        mime = content_type.split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/png"

        b64 = base64.b64encode(resp.content).decode()
        data_url = f"data:{mime};base64,{b64}"
        logger.info(f"Image downloaded: {len(resp.content)} bytes, mime={mime}")

        # Call vision model
        client_kwargs = dict(
            api_key=OPENAI_API_KEY or "none",
            base_url=OPENAI_API_BASE_VISION,
        )
        if PROXY_SERVER:
            client_kwargs["http_client"] = httpx.AsyncClient(proxy=PROXY_SERVER)

        client = AsyncOpenAI(**client_kwargs)
        try:
            result = await client.chat.completions.create(
                model=MODEL_VISION,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": p.question},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                max_completion_tokens=2000,
            )
            content = result.choices[0].message.content
            if content:
                return content
            return "Could not obtain an image description."
        except Exception as e:
            logger.error(f"Vision model call failed: {e}")
            return f"Image analysis failed: {str(e)}"
