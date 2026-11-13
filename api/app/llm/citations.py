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
# Sentence splitter that keeps "[n]" markers attached to their sentence.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[^\s])")


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


# --- v2.5 P0-4: claim-level support check (docs/20) --------------------------


@dataclass(slots=True)
class ClaimCheck:
    """Sentences whose [n] markers are NOT supported by the cited chunks."""

    unsupported_markers: list[int]
    flagged_sentences: list[str]


def split_sentences(text: str) -> list[str]:
    """Split into sentences; markdown list/table lines stay their own unit."""
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("#", "|", "-", "*")) or (
            len(line) < 200 and not _SENTENCE_RE.search(line)
        ):
            sentences.append(line)
            continue
        sentences.extend(p for p in _SENTENCE_RE.split(line) if p.strip())
    return sentences


def lexical_entailment(sentence: str, context: str) -> float:
    """Fraction of the sentence's content words present in the context (0..1).

    A cheap, dependency-free entailment proxy (RAGTruth/HHEM-style claim check
    without a model). Numbers and rare tokens count double via exact match.
    """
    from app.retrieval.scoring import tokenize

    s_tokens = tokenize(sentence)
    if not s_tokens:
        return 1.0  # nothing checkable (e.g. a bare citation line) — do not flag
    c_tokens = set(tokenize(context))
    c_lower = context.lower()
    hits = 0
    for t in s_tokens:
        if t in c_tokens or (len(t) >= 4 and t in c_lower):
            hits += 1
    return hits / len(s_tokens)


def check_claims(
    text: str,
    included: list[tuple[int, Chunk]],
    *,
    threshold: float = 0.34,
) -> ClaimCheck:
    """For every sentence carrying [n] markers, verify the cited chunks actually
    support it (max lexical entailment over the cited chunks >= threshold).

    Unsupported markers are REPORTED, never silently removed — the answer text
    is unchanged; the API surfaces them as ``unsupported_markers`` so clients
    can show the weak spots (docs/20 P0-4).
    """
    by_n = dict(included)
    unsupported: set[int] = set()
    flagged: list[str] = []
    for sentence in split_sentences(text):
        markers = [int(m) for m in _CITATION_RE.findall(sentence)]
        if not markers:
            continue
        cited = [by_n[n] for n in markers if n in by_n]
        if not cited:
            continue  # already stripped as invalid by validate_citations
        best = max(lexical_entailment(sentence, c.content) for c in cited)
        if best < threshold:
            unsupported.update(n for n in markers if n in by_n)
            flagged.append(sentence[:300])
    return ClaimCheck(unsupported_markers=sorted(unsupported), flagged_sentences=flagged)
