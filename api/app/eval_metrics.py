"""Retrieval + answer metrics for the v2 eval harness (docs/20 P0-8, v2.5.0).

Pure functions (no I/O) so they are unit-testable and reusable from both
``scripts/eval_run.py`` (live API) and ``scripts/eval_v2.py``:

- hit_at_k / recall_at_k / mrr / ndcg_at_k over ranked URLs vs ``relevant`` substrings
- context_precision / context_recall (RAGAS-style: cited sources vs relevant set)
- claim_support_rate: fraction of cited sentences that pass lexical entailment
  (reuses app.llm.citations.check_claims)
"""

from __future__ import annotations

import math
import re

_CITE = re.compile(r"\[(\d{1,3})\]")


def _matches(url: str, relevant: list[str]) -> bool:
    u = url.lower()
    return any(rel.lower() in u for rel in relevant)


def hit_at_k(urls: list[str], relevant: list[str], k: int = 5) -> float:
    if not relevant:
        return 0.0
    return 1.0 if any(_matches(u, relevant) for u in urls[:k]) else 0.0


def topical_match(query: str, text: str) -> bool:
    """docs/36 E1: does a result's title/snippet speak to the query at all?

    Domain-only labels ("vnexpress.net") credit ANY page of that site — the 26/11
    VI eval counted "chanh ngón tay giá 2 triệu" as a hit for "cách nấu phở bò".
    Content words (stopwords removed) must overlap: 1 for short queries, 2 from 4
    terms. Accents are kept when the query has them ("nội" != "nơi").
    """
    from app.retrieval.scoring import fold_diacritics, tokenize

    fold = query == fold_diacritics(query)
    q_terms = {t for t in tokenize(query, fold=fold) if len(t) >= 2}
    if not q_terms or not text:
        return False
    need = 1 if len(q_terms) <= 3 else 2
    return len(q_terms & set(tokenize(text, fold=fold))) >= need


def strict_hit_at_k(
    items: list[tuple[str, str]], relevant: list[str], query: str, k: int = 5
) -> float:
    """hit@k where the hit must match a relevant domain AND be on topic."""
    if not relevant:
        return 0.0
    return (
        1.0
        if any(_matches(u, relevant) and topical_match(query, text) for u, text in items[:k])
        else 0.0
    )


def recall_at_k(urls: list[str], relevant: list[str], k: int = 10) -> float:
    if not relevant:
        return 0.0
    top = urls[:k]
    hit = sum(1 for rel in relevant if any(_matches(u, [rel]) for u in top))
    return hit / len(relevant)


def mrr(urls: list[str], relevant: list[str]) -> float:
    for i, u in enumerate(urls, start=1):
        if _matches(u, relevant):
            return 1.0 / i
    return 0.0


def ndcg_at_k(urls: list[str], relevant: list[str], k: int = 10) -> float:
    if not relevant:
        return 0.0
    matched_rels: set[str] = set()
    gains: list[float] = []
    for u in urls[:k]:
        u_low = u.lower()
        gain = 0.0
        for rel in relevant:
            rel_low = rel.lower()
            if rel_low not in matched_rels and rel_low in u_low:
                matched_rels.add(rel_low)
                gain = 1.0
                break
        gains.append(gain)
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = [1.0] * min(len(relevant), k)
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return min(1.0, dcg / idcg) if idcg > 0 else 0.0


def context_precision(cited_urls: list[str], relevant: list[str]) -> float:
    """Share of cited sources that are relevant (RAGAS context precision, binary)."""
    if not cited_urls:
        return 0.0
    return sum(1 for u in cited_urls if _matches(u, relevant)) / len(cited_urls)


def context_recall(cited_urls: list[str], relevant: list[str]) -> float:
    """Share of relevant sources that were cited (RAGAS context recall, binary)."""
    if not relevant:
        return 0.0
    return sum(1 for rel in relevant if any(_matches(u, [rel]) for u in cited_urls)) / len(relevant)


def cited_urls_from_answer(answer_md: str, citations: list[dict]) -> list[str]:
    """URLs of the [n] markers actually present in the answer text."""
    used = {int(m) for m in _CITE.findall(answer_md)}
    return [str(c.get("url") or "") for c in citations if int(c.get("n") or 0) in used]


def claim_support_rate(
    answer_md: str, chunk_contents: dict[int, str], threshold: float = 0.34
) -> float:
    """Fraction of cited sentences supported by their cited chunk (lexical NLI proxy).

    ``chunk_contents`` maps citation n -> chunk text. Sentences without markers
    are ignored; an answer with no citations scores 0 (nothing verifiable).
    """
    from app.llm.citations import lexical_entailment, split_sentences

    checked = 0
    supported = 0
    for sentence in split_sentences(answer_md):
        markers = [int(m) for m in _CITE.findall(sentence)]
        if not markers:
            continue
        contexts = [chunk_contents[n] for n in markers if n in chunk_contents]
        if not contexts:
            continue
        checked += 1
        if max(lexical_entailment(sentence, c) for c in contexts) >= threshold:
            supported += 1
    return supported / checked if checked else 0.0


def numeric_fidelity_rate(answer_md: str, chunk_contents: dict[int, str]) -> float:
    """Share of numeric claims in cited sentences present in their cited chunk texts."""
    if not answer_md or not chunk_contents:
        return 0.0 if answer_md else 1.0
    from app.extraction.chunker import Chunk
    from app.llm.citations import verify_numeric_fidelity

    included = [
        (
            n,
            Chunk(
                id=str(n),
                document_url="",
                position=n,
                heading_path="",
                content=txt,
                word_count=len(txt.split()),
            ),
        )
        for n, txt in chunk_contents.items()
    ]
    fidelity, _unverified = verify_numeric_fidelity(answer_md, included)
    return fidelity


def citation_precision_rate(answer_md: str, included_ids: set[int] | list[int]) -> float:
    """Fraction of inline [n] citation markers that map to real included sources."""
    markers = [int(m) for m in _CITE.findall(answer_md or "")]
    if not markers:
        return 0.0
    valid_set = set(included_ids)
    hits = sum(1 for m in markers if m in valid_set)
    return round(hits / len(markers), 4)


def latency_percentiles(latencies_ms: list[float]) -> dict[str, float]:
    """Compute p50, p95, p99 latency in milliseconds over observed queries."""
    clean = sorted(float(x) for x in latencies_ms if x >= 0)
    if not clean:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def _pct(p: float) -> float:
        idx = min(len(clean) - 1, max(0, math.ceil(p * len(clean)) - 1))
        return round(clean[idx], 2)

    return {"p50_ms": _pct(0.50), "p95_ms": _pct(0.95), "p99_ms": _pct(0.99)}


def aggregate(records: list[dict]) -> dict:
    """Mean of each metric over per-query records."""
    if not records:
        return {}
    keys = (
        "hit_at_5",
        "hit_at_10",
        "recall_at_10",
        "mrr",
        "ndcg_at_10",
        "context_precision",
        "context_recall",
        "claim_support",
        "numeric_fidelity",
        "citation_precision",
        "strict_hit_at_5",
        "strict_hit_at_10",
        "answer_ok",
    )
    out: dict = {}
    for k in keys:
        vals = [float(r[k]) for r in records if r.get(k) is not None]
        out[k] = round(sum(vals) / len(vals), 4) if vals else 0.0
    out["n_queries"] = len(records)
    return out


def aggregate_by_slice(records: list[dict], key: str = "slice") -> dict[str, dict]:
    """Group records by ``key`` and compute aggregate metrics per slice."""
    grouped: dict[str, list[dict]] = {}
    for rec in records:
        label = str(rec.get(key) or "unknown")
        grouped.setdefault(label, []).append(rec)
    return {label: aggregate(group) for label, group in sorted(grouped.items())}
