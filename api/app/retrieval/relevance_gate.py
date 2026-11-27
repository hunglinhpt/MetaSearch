"""Off-topic result gate (docs/36 §20 Q1, v4.7.1).

Why: the keyless Lite web sources match on boilerplate. For "what is PostgreSQL
logical replication and how does it differ from physical replication", mwmbl
answered with "What is a VNC and how does it differ from a VPN?", "What is ray
tracing, and how does it differ…" — 9 of 10 results shared only "differ". They
were scraped (and the one relevant page was cancelled by the scrape early exit),
so the answer had nothing to stand on.

Rule (replayed on golden_v2 + golden_v3_vi, 540 labelled results): a result is
off-topic when ALL hold —

- its keyword relevance is below ``ABS_FLOOR`` (absolute), and
- below ``REL_RATIO`` x the best relevance in the same batch (relative: a weak
  batch keeps its best results), and
- it does not contain any *anchor* of the query: a named entity / code the user
  typed (mixed case "PostgreSQL", acronym "BERT", digits "10-K", a capitalised
  non-initial word "Stream").

Replay: 157 results dropped, 0 of the 52 gold-URL results lost. Without the
anchor guard the same thresholds lost 9 gold results (e.g. "Docs - Redis" for a
Vietnamese Redis question), so the guard is not optional.

The best result of a batch is never off-topic (kw >= REL_RATIO x kw), so the
gate cannot empty a non-empty list.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from app.retrieval.scoring import (
    compute_batch_idf,
    keyword_relevance,
    tokenize,
    tokenize_dual,
)

ABS_FLOOR = 0.3
REL_RATIO = 0.4

# Sentence-initial words that are capitalised only because they start the query.
_STARTERS = frozenset(
    {
        # en
        "what",
        "how",
        "why",
        "when",
        "where",
        "which",
        "who",
        "is",
        "are",
        "can",
        "does",
        "do",
        "the",
        "a",
        "an",
        "best",
        "latest",
        "compare",
        "difference",
        "explain",
        # vi (folded)
        "cach",
        "lam",
        "huong",
        "tai",
        "vi",
        "khi",
        "co",
        "la",
        "nhung",
        "cac",
        "tin",
        "gia",
        "so",
        "phan",
        "thu",
        "kien",
        "khai",
        "tong",
        "tim",
        "danh",
        "xu",
        "quy",
        "luat",
        "nen",
        "uu",
    }
)
# Question scaffolding the shared tokenizer keeps ("what", "how", "does"): every
# "what is X and how does it differ" title matches them, so they would lift the
# boilerplate results this gate exists to catch. Gate-local; ranking unchanged.
_QUESTION_WORDS = frozenset(
    {
        "what",
        "how",
        "why",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "does",
        "do",
        "did",
        "is",
        "are",
        "was",
        "were",
        "can",
        "could",
        "should",
        "would",
        "will",
        "it",
    }
)
_RAW_TOKEN_RE = re.compile(r"[^\W_][\w.+#-]*", re.UNICODE)


def query_anchors(query: str) -> set[str]:
    """Folded tokens of the entities/codes the user typed (may be empty)."""
    anchors: set[str] = set()
    for i, raw in enumerate(_RAW_TOKEN_RE.findall(query or "")):
        has_digit = any(c.isdigit() for c in raw)
        upper_inner = any(c.isupper() for c in raw[1:])
        title_case = raw[:1].isupper()
        if not (has_digit and len(raw) >= 2) and not upper_inner and not title_case:
            continue
        toks = tokenize(raw)
        if i == 0 and title_case and not upper_inner and not has_digit:
            toks = [t for t in toks if t not in _STARTERS]
        anchors.update(toks)
    return anchors


def _contains_anchor(anchors: set[str], text: str) -> bool:
    if not anchors:
        return False
    toks = set(tokenize(text))
    return any(a in toks or (len(a) >= 4 and any(t.startswith(a) for t in toks)) for a in anchors)


def off_topic_flags(query: str, items: Sequence[Any]) -> list[bool]:
    """One flag per item (``title``/``snippet``/``url`` attributes), batch-relative."""
    if len(items) < 2:
        return [False] * len(items)
    terms, accented = tokenize_dual(query)
    terms = [t for t in terms if t not in _QUESTION_WORDS]
    accented = [t for t in accented if t not in _QUESTION_WORDS]
    if len(terms) < 2:
        return [False] * len(items)  # one-word queries: relevance is too coarse
    idf = compute_batch_idf(terms, [(i.title or "", i.snippet or "") for i in items])
    kws = [
        keyword_relevance(terms, i.title or "", i.snippet or "", accented_terms=accented, idf=idf)
        for i in items
    ]
    best = max(kws)
    anchors = query_anchors(query)
    flags: list[bool] = []
    for item, kw in zip(items, kws, strict=True):
        if kw >= ABS_FLOOR or kw >= REL_RATIO * best:
            flags.append(False)
            continue
        text = f"{item.title or ''} {item.snippet or ''} {getattr(item, 'url', '') or ''}"
        flags.append(not _contains_anchor(anchors, text))
    return flags


def drop_off_topic(query: str, items: list[Any]) -> tuple[list[Any], int]:
    """Items kept for scraping/context, and how many were dropped."""
    flags = off_topic_flags(query, items)
    kept = [i for i, off in zip(items, flags, strict=True) if not off]
    return kept, len(items) - len(kept)
