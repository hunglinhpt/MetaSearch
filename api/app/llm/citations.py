"""Post-generation citation validation (docs/03-search-pipeline.md stage 11).

The LLM is instructed to cite ``[n]``; this module maps those markers back to
real chunks, strips anything bogus, and reports which sources were actually
used so the API can persist validated citations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.extraction.chunker import Chunk

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
# Sentence splitter that keeps "[n]" markers attached to their sentence.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[^\s])")
_NUM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:\$|US\$|€|£)?\d+(?:[.,]\d+)*(?:\s*(?:%|x|bps|tỷ|triệu|nghìn|ngàn|b|m|k))?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_DIGIT_RE = re.compile(r"\d")


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
    numeric_fidelity: float = 1.0
    unverified_numbers: list[str] = field(default_factory=list)


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


def extract_numeric_tokens(text: str) -> list[str]:
    """Extract normalized financial/quantitative figures (ignoring bare citation tags/dates)."""
    out: list[str] = []
    seen: set[str] = set()
    # Strip citation brackets [1] and markdown URLs before extracting numbers.
    cleaned = _CITATION_RE.sub("", re.sub(r"https?://\S+", "", text or ""))
    for m in _NUM_TOKEN_RE.findall(cleaned):
        norm = m.strip().lower().replace(",", "").replace(" ", "")
        # Skip plain 4-digit calendar years and trivial single digits like "1", "2".
        if re.fullmatch(r"(?:19|20)\d{2}", norm):
            continue
        if len(norm) == 1 and norm.isdigit():
            continue
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def verify_numeric_fidelity(
    text: str, included: list[tuple[int, Chunk]]
) -> tuple[float, list[str]]:
    """Share of numeric claims in cited sentences present in their cited chunks."""
    by_n = dict(included)
    total = 0
    matched = 0
    unverified: list[str] = []
    for sentence in split_sentences(text):
        markers = [int(m) for m in _CITATION_RE.findall(sentence)]
        if not markers:
            continue
        cited = [by_n[n] for n in markers if n in by_n]
        if not cited:
            continue
        nums = extract_numeric_tokens(sentence)
        if not nums:
            continue
        combined_ctx = " ".join(c.content.lower().replace(",", "") for c in cited)
        ctx_compact = combined_ctx.replace(" ", "")
        for tok in nums:
            total += 1
            bare = re.sub(r"(?:%|x|bps|tỷ|triệu|nghìn|ngàn|\$|us\$|€|£)", "", tok)
            if tok in ctx_compact or (bare and bare in combined_ctx):
                matched += 1
            elif tok not in unverified:
                unverified.append(tok)
    return (round(matched / total, 4) if total > 0 else 1.0), unverified


def lexical_entailment(sentence: str, context: str) -> float:
    """Fraction of the sentence's content words present in the context (0..1).

    Numbers and domain codes carry 2x weight relative to regular words.
    """
    from app.retrieval.scoring import tokenize

    s_tokens = tokenize(sentence)
    if not s_tokens:
        return 1.0  # nothing checkable (e.g. a bare citation line) — do not flag
    c_tokens = set(tokenize(context))
    c_lower = context.lower().replace(",", "")
    weighted_hits = 0.0
    total_weight = 0.0
    for t in s_tokens:
        w = 2.0 if _DIGIT_RE.search(t) else 1.0
        total_weight += w
        t_clean = t.replace(",", "")
        if t in c_tokens or t_clean in c_lower or (len(t) >= 4 and t in c_lower):
            weighted_hits += w
    return weighted_hits / total_weight if total_weight > 0 else 1.0


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
    num_fid, unverified = verify_numeric_fidelity(text, included)
    return ClaimCheck(
        unsupported_markers=sorted(unsupported),
        flagged_sentences=flagged,
        numeric_fidelity=num_fid,
        unverified_numbers=unverified,
    )
