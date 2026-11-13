"""Agent-friendly extras for POST /v1/search (docs/20 P0-7, v2.5.0).

``include=["contents","highlights","answer"]`` adds, on the TOP results only:
- ``contents``: scraped markdown of the page (bounded chars), via the existing
  scraper (SSRF/robots/caps all apply unchanged);
- ``highlights``: top extractive sentences per result (keyword overlap with the
  query, no LLM) — the cheap, honest version of Exa's highlights;
- ``answer``: one short instant answer synthesized over the top titles/snippets
  (response-level field), only when an LLM is configured.

Every extra is best-effort: any failure omits the field (None), never fails the
request. Extras are computed AFTER the search cache, so cached base results get
fresh extras on every call.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.config import Settings
from app.core.logging import get_logger
from app.extraction.scraper import PageContent, Scraper
from app.retrieval.models import ScoredResult
from app.retrieval.scoring import keyword_relevance, tokenize

logger = get_logger(__name__)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_HIGHLIGHT_MAX_CHARS = 300


def extract_highlights(text: str, query: str, *, max_highlights: int) -> list[str]:
    """Top sentences by query-term overlap (title gets 2x weight). Extractive only."""
    if not text or not query.strip():
        return []
    terms = tokenize(query)
    candidates: list[tuple[float, str]] = []
    for raw_line in text.splitlines():
        for sent in _SENTENCE_RE.split(raw_line.strip()):
            sent = sent.strip()
            if len(sent) < 20 or len(sent) > _HIGHLIGHT_MAX_CHARS:
                continue
            score = keyword_relevance(terms, sent, "")
            if score > 0:
                candidates.append((score, sent))
    candidates.sort(key=lambda p: -p[0])
    seen: set[str] = set()
    out: list[str] = []
    for _score, sent in candidates:
        key = sent.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sent)
        if len(out) >= max_highlights:
            break
    return out


async def build_extras(
    results: list[ScoredResult],
    query: str,
    include: list[str],
    *,
    settings: Settings,
    scraper: Scraper,
    gateway: Any | None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Return (per-url extras, instant_answer). Per-url extras hold any of
    contents/highlights; both default to absent on failure."""
    extras: dict[str, dict[str, Any]] = {}
    if not results or not include:
        return extras, None

    want_contents = "contents" in include
    want_highlights = "highlights" in include
    want_answer = "answer" in include

    top = results[: settings.search_extras_max_urls]
    contents: dict[str, str] = {}

    if want_contents or want_highlights:
        try:
            pages = await asyncio.wait_for(
                scraper.scrape_many([r.url for r in top]), timeout=settings.scrape_timeout_s
            )
            for page in pages:
                if isinstance(page, PageContent):
                    md = (page.markdown or "").strip()
                    contents[page.url] = md[: settings.search_extras_max_chars]
        except Exception as exc:
            logger.info("search_extras_scrape_failed", error=type(exc).__name__)

    for r in top:
        entry: dict[str, Any] = {}
        content = contents.get(r.url)
        if want_contents and content:
            entry["contents"] = content
        if want_highlights:
            source = content or r.snippet or ""
            # Title terms weigh in by prepending the title to the highlight source.
            hl = extract_highlights(
                f"{r.title}. {source}", query, max_highlights=settings.search_highlights_max
            )
            if hl:
                entry["highlights"] = hl
        if entry:
            extras[r.url] = entry

    instant: str | None = None
    if want_answer and gateway is not None and getattr(gateway, "provider", "none") != "none":
        try:
            listing = "\n".join(
                f"- {r.title} ({r.url}): {(r.snippet or '')[:300]}" for r in results[:5]
            )
            text, _usage = await gateway.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Answer the user's query in at most 3 short sentences using ONLY "
                            "the search results below. If they do not contain the answer, "
                            "say so in one sentence. Same language as the query."
                        ),
                    },
                    {"role": "user", "content": f"Query: {query}\n\nResults:\n{listing}"},
                ],
                max_tokens=220,
                task="instant_answer",
            )
            instant = text.strip() or None
        except Exception as exc:
            logger.info("search_extras_answer_failed", error=type(exc).__name__)

    return extras, instant
