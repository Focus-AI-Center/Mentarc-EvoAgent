"""HTML Render plugin — renders LLM-generated HTML in the frontend preview panel.

The agent generates HTML content (charts, dashboards, interactive pages, etc.)
and this tool saves it to workspace storage, returning a lightweight reference. The frontend
fetches the HTML on demand when the user clicks the preview card, keeping
conversation history and LLM context free of large HTML payloads.
"""

import json
import time

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from utils.logger import logger
from utils.workspace import Workspace


class HtmlRenderParams(BaseModel):
    title: str = Field(
        default="Preview",
        description="预览面板标题，如 '销售趋势图'、'产品对比表'",
    )
    html: str = Field(
        description="完整的 HTML 内容（可包含 <style>、<script>、CDN 引用等）",
    )


@register_plugin(
    PluginMeta(
        name="html_render",
        category="generation",
        capabilities=["html-render", "visualization", "interactive-page"],
        input_types=["text"],
        output_types=["html"],
        description="Render HTML content in a preview panel (charts, dashboards, interactive pages)",
    )
)
class HtmlRenderTool(BaseTool):
    name: str = "html_render"
    description: str = (
        "在前端预览面板中渲染 HTML 页面。适用于：\n"
        "- 数据可视化（图表、仪表盘）\n"
        "- 交互式内容（表单、计算器）\n"
        "- 富文本报告（带样式的 HTML 页面）\n"
        "- 产品展示页、对比表等\n"
        "传入完整 HTML（可含 <style>、<script>、CDN 引用如 Chart.js/ECharts/Tailwind）。"
    )
    param_anno = HtmlRenderParams

    def __init__(self, user_id: str = "", workspace=None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._workspace = workspace

    async def __call__(self, params: HtmlRenderParams) -> str:
        html = params.html.strip()
        title = params.title.strip() or "Preview"

        if not html:
            return "请提供要渲染的 HTML 内容。"

        # Wrap in full HTML document if not already
        if "<html" not in html.lower() and "<body" not in html.lower():
            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
</head>
<body>
{html}
</body>
</html>"""

        size_kb = round(len(html.encode("utf-8")) / 1024, 1)

        # Save HTML to workspace storage (separate from conversation history)
        preview_id = f"hp_{int(time.time())}_{id(html) % 10000:04d}"
        if self._user_id:
            try:
                ws = self._workspace or Workspace(self._user_id)
                ws.preview_write(preview_id, html)
                logger.info(
                    f"html_render: saved preview '{preview_id}' ({size_kb}KB) for user {self._user_id}"
                )
            except Exception as e:
                logger.error(f"html_render: failed to save preview: {e}")
                return f"HTML 预览保存失败: {e}"
        else:
            logger.warning("html_render: no user_id, preview not persisted")

        # Return lightweight reference (no HTML in conversation history)
        return json.dumps(
            {
                "__html_render__": True,
                "preview_id": preview_id,
                "title": title,
                "size_kb": size_kb,
            },
            ensure_ascii=False,
        )
