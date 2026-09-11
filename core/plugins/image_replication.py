"""Image replication plugin — replicate product photo style via Doubao API.

Workflow (single-image, max_images=1): analyze competitor → extract style → generate
Workflow (suite, max_images>1): load product image → generate e-commerce display set
"""

import base64
import json
import os

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from configs.settings import (
    DOUBAO_API_KEY,
    DOUBAO_MODEL,
    MODEL_VISION,
    OPENAI_API_BASE_VISION,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from core.plugins.image_editing import ImageEditingParams, ImageEditingTool
from utils.image_upload import upload_image
from utils.logger import logger

DOUBAO_API_BASE = os.getenv("DOUBAO_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
_MAX_RETRIES = 2
_TIMEOUT = 300.0

_RATIO_TO_SIZE = {
    "1:1": "2048x2048",
    "4:3": "2304x1728",
    "3:4": "1728x2304",
    "16:9": "2560x1440",
    "9:16": "1440x2560",
}

SUITE_PROMPT = (
    "生成四张适合电商展示的图像，保持原产品设计细节完全不变，"
    "85%产品主体居中白底图展示，2个不同的角度图展示，细节图展示，"
    "多种静态展示方式，画面干净高清，禁止出现水印文字。"
)

REPLICATION_PROMPT = (
    "Replicate the visual style of a reference product photo onto the user's product. "
    "Keep the user's product design exactly unchanged. Match: background={background}, "
    "surface={surface}, lighting={lighting}, composition={composition}, "
    "color_tone={color_tone}, props={props}. Generate a professional e-commerce photo."
)


async def _analyze_and_extract(image_url: str) -> tuple[dict, str]:
    """Analyze competitor image style using vision LLM."""
    if PROXY_SERVER:
        client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE_VISION,
            http_client=httpx.AsyncClient(proxy=PROXY_SERVER),
        )
    else:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE_VISION)

    prompt = (
        "Analyze this product photo's visual style. Extract these fields as JSON:\n"
        '{"background": "...", "surface": "...", "lighting": "...", '
        '"composition": "...", "color_tone": "...", "props": "..."}\n'
        "Keep each field under 20 words. Be specific about colors, materials, angles."
    )

    try:
        resp = await client.chat.completions.create(
            model=MODEL_VISION,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            max_completion_tokens=500,
        )
        raw = resp.choices[0].message.content or ""
        # Extract JSON from response
        import re

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            fields = json.loads(m.group())
        else:
            fields = {
                "background": "white",
                "surface": "flat",
                "lighting": "soft",
                "composition": "centered",
                "color_tone": "neutral",
                "props": "none",
            }
        return fields, raw
    except Exception as e:
        logger.error(f"Style analysis failed: {e}")
        return {
            "background": "white",
            "surface": "flat",
            "lighting": "soft",
            "composition": "centered",
            "color_tone": "neutral",
            "props": "none",
        }, str(e)


async def _load_image(source: str, user_id: str = "", workspace=None) -> tuple[str, str]:
    """Load image from URL. Returns (b64, mime)."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(source)
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
        if mime not in ("image/png", "image/jpeg", "image/webp"):
            mime = "image/png"
        return base64.b64encode(resp.content).decode("utf-8"), mime


class ImageReplicationParams(BaseModel):
    competitor_photo_url: str = Field(default="", description="竞品图片 URL（单图复刻时必填）")
    user_product_image: str = Field(description="用户产品图 URL")
    max_images: int = Field(default=1, description="生成图片数量（1=单图复刻，4=套图生成）")
    aspect_ratio: str = Field(default="1:1", description="输出宽高比")


@register_plugin(
    PluginMeta(
        name="image_replication",
        category="generation",
        capabilities=["image-replication"],
        input_types=["image_url"],
        output_types=["image_url"],
        description="Replicate product photo style or generate e-commerce display set",
    )
)
class ImageReplicationTool(BaseTool):
    name: str = "image_replication"
    description: str = (
        "产品图片复刻工具。两种模式：\n"
        "1. 单图风格复刻（max_images=1）：分析竞品图风格，生成同风格的用户产品图\n"
        "2. 电商套图（max_images=4）：生成 4 张电商展示图"
    )
    param_anno = ImageReplicationParams
    user_id: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.user_id = kwargs.get("user_id", "")
        self._workspace = kwargs.get("workspace", None)

    async def __call__(self, params: ImageReplicationParams) -> str:
        if not self.user_id:
            return json.dumps({"error": "user_id required"})

        if params.max_images > 1:
            return await self._suite_generate(params)
        return await self._style_replicate(params)

    async def _suite_generate(self, params: ImageReplicationParams) -> str:
        """Generate multiple e-commerce display images."""
        if not DOUBAO_API_KEY:
            return json.dumps({"error": "DOUBAO_API_KEY not configured"})
        try:
            b64, mime = await _load_image(params.user_product_image, self.user_id, self._workspace)
            data_uri = f"data:{mime};base64,{b64}"

            url = os.getenv("DOUBAO_EDITS_URL") or f"{DOUBAO_API_BASE}/images/edits"
            size = _RATIO_TO_SIZE.get(params.aspect_ratio, "2048x2048")
            body = {
                "model": DOUBAO_MODEL,
                "prompt": SUITE_PROMPT,
                "image": [data_uri],
                "response_format": "b64_json",
                "size": size,
                "n": min(params.max_images, 4),
            }
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"}

            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=body)

            if resp.status_code != 200:
                return json.dumps({"error": f"HTTP {resp.status_code}"})

            data_list = resp.json().get("data", [])
            results = []
            for i, item in enumerate(data_list):
                if item.get("b64_json"):
                    img_bytes = base64.b64decode(item["b64_json"])
                    img_url = upload_image(img_bytes, ext="png", user_id=self.user_id)
                    results.append({"index": i, "image_path": img_url})

            return json.dumps(
                {
                    "images": results,
                    "count": len(results),
                    "mode": "suite",
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"Suite generation error: {e}")
            return json.dumps({"error": str(e)})

    async def _style_replicate(self, params: ImageReplicationParams) -> str:
        """Single-image style replication from competitor photo."""
        try:
            if not params.competitor_photo_url:
                return json.dumps({"error": "单图复刻需要 competitor_photo_url"})

            # Analyze competitor style
            style_fields, raw_analysis = await _analyze_and_extract(params.competitor_photo_url)
            prompt = REPLICATION_PROMPT.format(**style_fields)

            # Generate via image editing
            editing_tool = ImageEditingTool(user_id=self.user_id, workspace=self._workspace)
            editing_params = ImageEditingParams(
                prompt=prompt,
                image_urls=[params.user_product_image],
                aspect_ratio=params.aspect_ratio,
            )
            editing_result_raw = await editing_tool(editing_params)
            editing_result = (
                editing_result_raw.output
                if hasattr(editing_result_raw, "output")
                else str(editing_result_raw)
            )

            try:
                result_data = json.loads(editing_result)
            except (json.JSONDecodeError, TypeError):
                result_data = {"raw_result": str(editing_result)}

            result_data["replication_meta"] = {
                "competitor_photo_url": params.competitor_photo_url,
                "style_analysis": raw_analysis,
                "style_fields": style_fields,
            }
            return json.dumps(result_data, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Style replication error: {e}")
            return json.dumps({"error": str(e)})
