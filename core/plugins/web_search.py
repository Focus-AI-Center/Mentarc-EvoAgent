"""Web search plugin — searches the web via Tavily API."""

from datetime import UTC

from pydantic import BaseModel, Field

from configs.settings import TAVILY_API_KEY
from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from utils.logger import logger


class WebSearchParams(BaseModel):
    query: str = Field(description="The search query string")
    max_results: int = Field(default=5, description="Maximum number of results to return")


@register_plugin(
    PluginMeta(
        name="web_search",
        category="search",
        capabilities=["web-search"],
        input_types=["text"],
        output_types=["text"],
        description="Search the web via Tavily API",
    )
)
class WebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = "Search the web for current information. Input should be a search query."
    param_anno = WebSearchParams

    async def __call__(self, params: WebSearchParams) -> str:
        from datetime import datetime

        try:
            if not TAVILY_API_KEY:
                return "Error: TAVILY_API_KEY not configured."

            from tavily import TavilyClient

            client = TavilyClient(api_key=TAVILY_API_KEY)
            response = client.search(query=params.query, max_results=params.max_results)

            results = response.get("results", [])
            if not results:
                return "No results found."

            body = "\n---\n".join(
                f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('content', '')[:500]}" for r in results
            )
            meta = (
                f"\n\n---\n**[数据元信息]** 来源: Tavily | "
                f"查询时间: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} | "
                f"结果数: {len(results)} | 查询词: {params.query}"
            )
            return body + meta
        except Exception as e:
            logger.error(f"WebSearch error: {e}")
            return f"Search failed: {str(e)}"
