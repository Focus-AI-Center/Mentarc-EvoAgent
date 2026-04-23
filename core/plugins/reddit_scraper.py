"""Reddit Scraper plugin - scrape Reddit discussions via Apify.

Uses Apify's Reddit Scraper Lite (trudax/reddit-scraper-lite) for data collection.
Returns raw JSON data directly.
"""

import asyncio
import json
import os
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.plugins.base import PluginMeta, register_plugin
from utils.logger import logger

# --- Settings ---
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
ACTOR_ID = "trudax~reddit-scraper-lite"


class RedditScraperParams(BaseModel):
    keywords: str = Field(
        description="English search keywords, comma-separated. e.g. 'smart home,home automation,best smart speaker'"
    )
    subreddits: str = Field(
        default="",
        description="Target subreddits, comma-separated (optional). e.g. 'smarthome,homeautomation'",
    )
    time_range: Literal["week", "month", "year", "all"] = Field(
        default="month", description="Time range: week/month/year/all"
    )
    max_items: int = Field(default=50, description="Max items to scrape (5-200)")


@register_plugin(
    PluginMeta(
        name="reddit_scraper",
        category="data",
        capabilities=["reddit-scraping", "social-media-analysis", "market-research"],
        input_types=["text"],
        output_types=["json"],
        description="Scrape Reddit discussions via Apify and return raw JSON data",
    )
)
class RedditScraperTool(BaseTool):
    name: str = "reddit_scraper"
    description: str = (
        "Scrape Reddit posts and comments by English keywords. "
        "Returns raw JSON data from Apify Reddit Scraper."
    )
    param_anno = RedditScraperParams

    async def __call__(self, params: RedditScraperParams) -> str:
        """Scrape Reddit and return raw JSON."""
        if not APIFY_API_TOKEN:
            return json.dumps({"error": "APIFY_API_TOKEN not configured"})

        searches = [k.strip() for k in params.keywords.split(",") if k.strip()]
        if not searches:
            return json.dumps({"error": "No keywords provided"})

        items = await self._scrape_reddit(
            searches=searches[:5],
            time_range=params.time_range,
            max_items=params.max_items,
        )

        return json.dumps(items, ensure_ascii=False)

    async def _scrape_reddit(
        self,
        searches: list[str],
        time_range: str,
        max_items: int,
    ) -> list[dict]:
        """Call Apify Reddit Scraper Lite actor and return raw items."""
        actor_input = {
            "searches": searches,
            "type": "posts",
            "sort": "relevance",
            "time": time_range,
            "maxItems": max_items,
            "maxPostCount": max_items,
            "maxComments": 20,
            "proxy": {"useApifyProxy": True},
        }

        base_url = "https://api.apify.com/v2"
        headers = {"Authorization": f"Bearer {APIFY_API_TOKEN}"}

        for attempt in range(3):
            try:
                logger.info(f"Apify Reddit Scraper (attempt {attempt + 1}/3), keywords: {searches}")

                async with httpx.AsyncClient(timeout=300) as client:
                    # Start actor run
                    run_resp = await client.post(
                        f"{base_url}/acts/{ACTOR_ID}/runs",
                        headers=headers,
                        json=actor_input,
                        timeout=60,
                    )
                    run_resp.raise_for_status()
                    run_data = run_resp.json()["data"]
                    run_id = run_data["id"]
                    dataset_id = run_data["defaultDatasetId"]

                    # Wait for completion
                    while True:
                        status_resp = await client.get(
                            f"{base_url}/actor-runs/{run_id}",
                            headers=headers,
                        )
                        status_resp.raise_for_status()
                        status = status_resp.json()["data"]["status"]

                        if status == "SUCCEEDED":
                            break
                        elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                            raise RuntimeError(f"Actor run {status}")

                        await asyncio.sleep(3)

                    # Fetch results
                    items_resp = await client.get(
                        f"{base_url}/datasets/{dataset_id}/items",
                        headers=headers,
                        params={"format": "json"},
                    )
                    items_resp.raise_for_status()
                    items = items_resp.json()

                logger.info(f"Scraping complete: {len(items)} items")
                return items

            except Exception as e:
                logger.error(f"Scraping failed (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(5)

        logger.error("All 3 scraping attempts failed")
        return []
