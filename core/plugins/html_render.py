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
        description="Preview title, such as 'Sales trends' or 'Product comparison'",
    )
    html: str = Field(
        description="Complete HTML, including optional styles, scripts and CDN resources",
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
        "Render an HTML page in the preview panel. Suitable for:\n"
        "- Data visualizations such as charts and dashboards\n"
        "- Interactive content such as forms and calculators\n"
        "- Styled HTML reports\n"
        "- Product pages and comparison tables\n"
        "Provide complete HTML; styles, scripts and CDN libraries such as Chart.js, ECharts or Tailwind are supported."
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
            return "Provide HTML content to render."

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
                return f"HTML preview save failed: {e}"
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
