"""Fetch URL plugin — extracts text content from web pages using httpx + BeautifulSoup."""

import re

import httpx
from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from utils.logger import logger


class FetchUrlParams(BaseModel):
    url: str = Field(description="The URL to fetch and extract text from")


@register_plugin(
    PluginMeta(
        name="fetch_url",
        category="data",
        capabilities=["web-scraping", "url-fetch"],
        input_types=["url"],
        output_types=["text"],
        description="Fetch and extract text content from a URL",
    )
)
class FetchUrlTool(BaseTool):
    name: str = "fetch_url"
    description: str = (
        "Fetch a web page and extract its text content. Input should be a valid URL (http/https)."
    )
    param_anno = FetchUrlParams

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def __call__(self, params: FetchUrlParams) -> str:
        url = params.url.strip()
        if not url:
            return "Error: URL is empty."

        # Normalize URL
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            text, title = await self._fetch_and_parse(url)
            if not text:
                return f"Failed to extract content from {url}"

            # Truncate to reasonable size
            if len(text) > 8000:
                text = text[:8000] + "\n\n... (truncated)"

            header = f"**{title}**\n{url}\n" if title else f"{url}\n"
            return header + text

        except httpx.TimeoutException:
            return f"Timeout fetching {url}"
        except Exception as e:
            logger.error(f"FetchUrl error: {e}")
            return f"Error fetching {url}: {str(e)}"

    @staticmethod
    async def _fetch_and_parse(url: str) -> tuple[str, str]:
        """Fetch URL and extract text using BeautifulSoup."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")

        # If JSON response, return formatted
        if "application/json" in content_type:
            return response.text[:8000], ""

        # Parse HTML
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(response.text, "html.parser")

        # Remove script and style elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        # Extract text
        text = soup.get_text(separator="\n", strip=True)

        # Clean up: collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text, title
