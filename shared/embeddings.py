"""Embeddings via gemini-embedding-001 (single provider — same key as the agent).

Output is MRL-truncated to 1536 dims to match the vector(1536) column, then
L2-normalised (Google recommends normalising sub-3072 embeddings so cosine
behaves correctly). Use RETRIEVAL_DOCUMENT for stored facts, RETRIEVAL_QUERY
for search queries.
"""
import math

from google import genai
from google.genai import types

from shared.config import settings

MODEL = "gemini-embedding-001"
DIM = 1536

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embed a batch of texts -> list of 1536-dim L2-normalised vectors."""
    if not texts:
        return []
    resp = _get_client().models.embed_content(
        model=MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=DIM
        ),
    )
    return [_l2_normalize(list(e.values)) for e in resp.embeddings]


def embed_one(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    return embed([text], task_type=task_type)[0]
