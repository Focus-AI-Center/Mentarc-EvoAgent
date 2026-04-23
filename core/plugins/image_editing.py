"""Image editing plugin — edit images using text instructions via Doubao API."""

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


class ImageEditingParams(BaseModel):
    prompt: str = Field(description="编辑指令（英文，描述要如何修改图片）")
    image_urls: list[str] = Field(description="要编辑的图片 URL 列表（支持 http/https URL）")
    aspect_ratio: str = Field(default="1:1", description="输出宽高比")


@register_plugin(
    PluginMeta(
        name="image_editing",
        category="generation",
        capabilities=["image-editing"],
        input_types=["text", "image_url"],
        output_types=["image_url"],
        description="Edit images using text instructions via Doubao API",
    )
)
class ImageEditingTool(BaseTool):
    name: str = "image_editing"
    description: str = "根据文本指令编辑图片，支持风格转换、图片增强、多图合成等。"
    param_anno = ImageEditingParams
    user_id: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.user_id = kwargs.get("user_id", "")
        self._workspace = kwargs.get("workspace", None)

    async def __call__(self, params: ImageEditingParams) -> str:
        if not DOUBAO_API_KEY:
            return "Error: DOUBAO_API_KEY not configured."
        if not self.user_id:
            return "Error: user_id required."

        # Load source images
        loaded = []
        for src in params.image_urls:
            try:
                b64, mime = await self._load_image(src)
                loaded.append(f"data:{mime};base64,{b64}")
            except Exception as e:
                return f"Failed to load image {src}: {e}"

        # Call Doubao edit API
        url = f"{DOUBAO_API_BASE}/images/edits"
        size = _RATIO_TO_SIZE.get(params.aspect_ratio, "2048x2048")
        body = {
            "model": DOUBAO_MODEL,
            "prompt": params.prompt,
            "image": loaded,
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
                    return f"Image editing failed: HTTP {resp.status_code}"

                data_list = resp.json().get("data", [])
                images_b64 = [item["b64_json"] for item in data_list if item.get("b64_json")]
                if not images_b64:
                    return "No image data in response"

                img_bytes = base64.b64decode(images_b64[0])
                img_url = upload_image(img_bytes, ext="png", user_id=self.user_id)
                return json.dumps(
                    {
                        "image_path": img_url,
                        "original_urls": params.image_urls,
                        "prompt": params.prompt,
                        "markdown": f"![编辑图片]({img_url})",
                    },
                    ensure_ascii=False,
                )

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
                return f"Image editing timeout ({_TIMEOUT}s)"
            except Exception as e:
                logger.error(f"Image editing error: {e}")
                return f"Image editing failed: {e}"

        return "Image editing failed: all retries exhausted"

    async def _load_image(self, source: str) -> tuple[str, str]:
        """Load image from URL, workspace, or local path. Returns (b64, mime)."""
        ext_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

        # Try workspace image by filename
        if self.user_id and "/" not in source and not source.startswith("http"):
            from utils.ws_call import ws_call

            ws = self._workspace
            if ws:
                data = (
                    await ws_call(ws, "image_read", source)
                    if hasattr(ws, "_send")
                    else ws.image_read(source)
                    if hasattr(ws, "image_read")
                    else None
                )
                if data:
                    ext = "." + source.rsplit(".", 1)[-1].lower() if "." in source else ".png"
                    return base64.b64encode(data).decode("utf-8"), ext_mime.get(ext, "image/png")

        # Remote URL
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(source)
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                mime = "image/png"
            return base64.b64encode(resp.content).decode("utf-8"), mime
