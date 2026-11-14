"""Optional vision embedding path for PDF pages (docs/20 P2-1).

Local page raster + ColPali/jina-v4 vision vectors need a GPU — out of scope for
the Lite profile (docs/20 §6 giants-first). What we provide here is the **API
path**: when ``VISION_EMBED_API_BASE_URL`` is set (a jina-embeddings-v4 or NeMo
extraction NIM endpoint), page texts (or rendered page images, when the caller
has them) are embedded through that API and stored as chunk vectors — the same
``chunks.embedding`` column the hybrid search reads, so ``POST /v1/hybrid``
becomes multimodal-capable without any local model. Empty base URL = disabled
(text-only page retrieval still works via pypdf).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.logging import get_logger
from app.http import get_http_client

logger = get_logger(__name__)


def vision_embed_available(settings: Any) -> bool:
    base = getattr(settings, "vision_embed_api_base_url", "").strip()
    # Operator-configured (env), not user input — but still only ever http(s):
    # httpx would reject other schemes anyway, fail fast with a clear bool.
    return base.startswith(("http://", "https://"))


async def embed_texts_via_vision_api(
    settings: Any, texts: list[str], *, api_key: str = ""
) -> list[list[float]]:
    """Embed texts through the configured vision/multimodal API (OpenAI-shape
    /embeddings). Raises on failure — callers must fail-open."""
    base = settings.vision_embed_api_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client: httpx.AsyncClient = get_http_client("internal")
    resp = await client.post(
        f"{base}/embeddings",
        json={"model": settings.vision_embed_model, "input": texts},
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = sorted(payload["data"], key=lambda item: item["index"])
    vectors = [[float(x) for x in item["embedding"]] for item in data]
    # The chunks.embedding column is fixed-dim (EMBEDDING_DIM): a vision model
    # with another output dim must fail loudly here (caller fails open), never
    # silently store vectors the hybrid search cannot compare.
    from app.db.base import EMBEDDING_DIM

    if any(len(v) != EMBEDDING_DIM for v in vectors):
        raise ValueError(f"vision embedding dim != {EMBEDDING_DIM} (check VISION_EMBED_MODEL)")
    return vectors
