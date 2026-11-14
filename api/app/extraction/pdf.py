"""PDF page-level extraction (docs/20 P2-1 — ColPali-lite, text side).

Financial PDFs (BCTC, annual reports) are page-structured: users ask "trang có
biểu đồ doanh thu quý" and expect a page number. We extract text per page with
``pypdf`` (pinned dependency) and emit markdown with ``## p.N`` headings, so the
existing chunker produces page-addressable chunks and citations can point at
``page=N``. True multimodal (render page → image → ColPali/jina-v4 vision vector)
needs a GPU or a vision API — see ``app/retrieval/vision.py`` for the optional
API path (``VISION_EMBED_API_BASE_URL``); without it we stay text-only, which
already covers the majority of "find the page with X" queries.
"""

from __future__ import annotations

import io

from app.core.logging import get_logger

logger = get_logger(__name__)


def is_pdf_bytes(body: bytes) -> bool:
    return body[:5] == b"%PDF-"


def pdf_pages_to_markdown(body: bytes, *, max_pages: int = 60, max_chars: int = 400_000) -> str:
    """Extract per-page text from PDF bytes; returns markdown with ``## p.N``
    section headings. Bounded by ``max_pages``/``max_chars``; any pypdf failure
    returns "" (caller falls back to the normal extract chain)."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a pinned dep
        logger.warning("pypdf_missing")
        return ""
    try:
        reader = PdfReader(io.BytesIO(body))
        n = min(len(reader.pages), max_pages)
        parts: list[str] = []
        total = 0
        for i in range(n):
            try:
                text = (reader.pages[i].extract_text() or "").strip()
            except Exception as exc:
                logger.info("pdf_page_extract_failed", page=i + 1, error=type(exc).__name__)
                text = ""
            if not text:
                continue
            parts.append(f"## p.{i + 1}\n\n{text}")
            total += len(text)
            if total > max_chars:
                parts.append("\n\n[truncated: max_chars reached]")
                break
        return "\n\n".join(parts)
    except Exception as exc:
        logger.warning("pdf_extract_failed", error=type(exc).__name__)
        return ""
