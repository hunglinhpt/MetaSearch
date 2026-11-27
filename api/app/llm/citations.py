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

# v4.5.0 (docs/34 audit round 4): numbers are matched as WHOLE tokens. Substring
# matching reported "95%" as present in "6,95%" and "95" in "1995", so fabricated
# figures passed both the answer numeric-fidelity check and claim verification.
_DIGIT_SEP_RE = re.compile(r"(?<=\d)[.,](?=\d)")
_TEXT_NUM_RE = re.compile(r"\d[\d.,]*(?:\s*%)?")
_UNIT_STRIP_RE = re.compile(r"^(?:us\$|\$|€|£)|(?:%|x|bps|tỷ|triệu|nghìn|ngàn|b|m|k)$")


def canon_number(text: str) -> str:
    """Drop separators BETWEEN digits so locale spellings compare equal:
    VI "7,09%" / EN "7.09%" -> "709%"; VI "1.000" / EN "1,000" -> "1000"."""
    return _DIGIT_SEP_RE.sub("", text)


def numbers_in(text: str) -> tuple[set[str], set[str]]:
    """Whole numeric tokens of ``text`` as ``(percentages, bare_numbers)``."""
    with_pct: set[str] = set()
    bare: set[str] = set()
    for m in _TEXT_NUM_RE.findall((text or "").lower()):
        canon = canon_number(m.replace(" ", "")).rstrip(".,")
        if canon.endswith("%"):
            with_pct.add(canon)
            bare.add(canon[:-1])
        else:
            bare.add(canon)
    return with_pct, bare


def number_present(token: str, numbers: tuple[set[str], set[str]]) -> bool:
    """Is a normalized figure (from :func:`extract_numeric_tokens`) present as a
    whole token? A percentage must appear AS a percentage."""
    with_pct, bare_set = numbers
    canon = canon_number(token.lower().replace(" ", ""))
    if canon.endswith("%"):
        return canon in with_pct
    bare = _UNIT_STRIP_RE.sub("", canon).rstrip(".,")
    return bool(bare) and bare in bare_set


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


def grounding_summary(text: str, claims: ClaimCheck) -> dict[str, object]:
    """docs/36 X1: how well is this answer grounded? (XERJ Query-1 declines with
    UNKNOWN/UNSUPPORTED instead of guessing; we never suppress an answer, but we
    state its grounding so a caller can choose to show, warn or discard.)

    status: ``grounded`` (every cited sentence supported, >= half the prose cited,
    numbers verified) | ``partial`` | ``weak`` (half or more of the cited
    sentences unsupported, or most figures unverified) | ``ungrounded`` (nothing
    cited).
    """
    prose = [
        s
        for s in split_sentences(text)
        if not s.startswith(("#", "|")) and len(_CITATION_RE.sub("", s).split()) >= 3
    ]
    cited = [s for s in prose if _CITATION_RE.search(s)]
    unsupported = len(claims.flagged_sentences)
    coverage = round(len(cited) / len(prose), 3) if prose else 0.0
    if not cited:
        status = "ungrounded"
    elif unsupported * 2 >= len(cited) or claims.numeric_fidelity < 0.5:
        status = "weak"
    elif unsupported or coverage < 0.5 or claims.unverified_numbers:
        status = "partial"
    else:
        status = "grounded"
    return {
        "status": status,
        "prose_sentences": len(prose),
        "cited_sentences": len(cited),
        "unsupported_sentences": unsupported,
        "citation_coverage": coverage,
        "numeric_fidelity": round(float(claims.numeric_fidelity), 3),
        "unverified_numbers": list(claims.unverified_numbers)[:10],
    }


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
        ctx_numbers = numbers_in(" ".join(c.content for c in cited))
        for tok in nums:
            total += 1
            if number_present(tok, ctx_numbers):
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
