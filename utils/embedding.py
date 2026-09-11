"""Embedding utilities — uses OpenAI text-embedding-3-small."""

import math
import os

from openai import OpenAI

from configs.settings import OPENAI_API_BASE, OPENAI_API_BASE_SECONDARY, OPENAI_API_KEY
from utils.logger import logger

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_API_BASE = os.getenv("EMBEDDING_API_BASE") or OPENAI_API_BASE_SECONDARY or OPENAI_API_BASE

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY, base_url=EMBEDDING_API_BASE)
    return _client


def get_embedding(text: str) -> list[float]:
    """Get embedding vector for a single text string."""
    try:
        client = _get_client()
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return []


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Get embedding vectors for multiple texts in one API call."""
    if not texts:
        return []
    try:
        client = _get_client()
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=texts,
        )
        return [item.embedding for item in response.data]
    except Exception as e:
        logger.error(f"Batch embedding failed: {e}")
        return [[] for _ in texts]
