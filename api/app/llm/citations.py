"""Post-generation citation validation (docs/03-search-pipeline.md stage 11).

The LLM is instructed to cite ``[n]``; this module maps those markers back to
real chunks, strips anything bogus, and reports which sources were actually
used so the API can persist validated citations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.extraction.chunker import Chunk

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


@dataclass(slots=True)
class ValidatedCitation:
    n: int
    chunk_id: str
    url: str
    title: str


@dataclass(slots=True)
class CitationCheck:
    text: str  # answer text with invalid markers stripped
    citations: list[ValidatedCitation]
    invalid_markers: list[int]


def validate_citations(text: str, included: list[tuple[int, Chunk]]) -> CitationCheck:
    """Strip citation markers that don't map to a numbered source chunk."""
    by_n = dict(included)
    used: dict[int, ValidatedCitation] = {}
    invalid: list[int] = []

    def _replace(match: re.Match[str]) -> str:
        n = int(match.group(1))
        chunk = by_n.get(n)
        if chunk is None:
            invalid.append(n)
            return ""
        if n not in used:
            used[n] = ValidatedCitation(
                n=n, chunk_id=chunk.id, url=chunk.document_url, title=chunk.source_title
            )
        return match.group(0)

    cleaned = _CITATION_RE.sub(_replace, text)
    # Collapse whitespace left behind by stripped markers, e.g. "word [9]." -> "word".
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return CitationCheck(
        text=cleaned,
        citations=sorted(used.values(), key=lambda c: c.n),
        invalid_markers=sorted(set(invalid)),
    )
