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
    gains = [1.0 if _matches(u, relevant) else 0.0 for u in urls[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = [1.0] * min(len(relevant), k)
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


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
    )
    out = {k: round(sum(r.get(k, 0.0) for r in records) / len(records), 4) for k in keys}
    out["n_queries"] = len(records)
    return out
