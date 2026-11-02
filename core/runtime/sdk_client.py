"""Shared, proxy-aware SDK client initialization."""

import httpx
from agents import set_default_openai_client
from openai import AsyncOpenAI

from configs.settings import OPENAI_API_BASE, OPENAI_API_KEY, PROXY_SERVER

_sdk_client_initialized = False


def _create_async_client() -> AsyncOpenAI:
    """Create a proxy-aware AsyncOpenAI client (for HistoryCompressor)."""
    if PROXY_SERVER:
        return AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
            http_client=httpx.AsyncClient(proxy=PROXY_SERVER),
        )
    return AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)


def _ensure_sdk_client():
    """Set up the default OpenAI client for the Agents SDK (once)."""
    global _sdk_client_initialized
    if _sdk_client_initialized:
        return
    if PROXY_SERVER:
        client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
            http_client=httpx.AsyncClient(proxy=PROXY_SERVER),
        )
    else:
        client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
        )
    set_default_openai_client(client)
    _sdk_client_initialized = True
