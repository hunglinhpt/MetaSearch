"""Embedding client for the L2 semantic cache (docs/03-search-pipeline.md §7).

OpenAI-compatible ``/embeddings`` (OpenAI, vLLM, TEI, Ollama). Vector dimension
is checked against the pgvector column before any write so a wrong model never
poisons the table.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.core.logging import get_logger
from app.db.base import EMBEDDING_DIM
from app.http import get_http_client

logger = get_logger(__name__)


class EmbeddingError(Exception):
    pass


class EmbeddingClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def enabled(self) -> bool:
        """L2 semantic cache is on only when EMBEDDING_L2 selects a backend."""
        return self._settings.embedding_l2 != "off"

    async def embed(self, text: str) -> list[float]:
        """Return one embedding vector; raises ``EmbeddingError`` on failure."""
        url = f"{self._settings.embedding_api_base_url.rstrip('/')}/embeddings"
        headers = {}
        if self._settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {self._settings.embedding_api_key}"
        try:
            client = self._client or get_http_client("internal")
            resp = await client.post(
                url,
                json={"model": self._settings.embedding_model, "input": [text]},
                headers=headers,
                timeout=10.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc

        try:
            vector = payload["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError("unexpected embedding response shape") from exc
        if len(vector) != EMBEDDING_DIM:
            raise EmbeddingError(
                f"embedding dim {len(vector)} != expected {EMBEDDING_DIM} (check EMBEDDING_MODEL)"
            )
        return [float(x) for x in vector]
