"""Adaptive complexity router (docs/20 P1-2, v2.6).

Classifies a query as ``simple`` / ``standard`` / ``deep`` with cheap
heuristics only (no LLM call by default — cost stays O(1) and the decision
is deterministic, so it can live inside the search cache key path).

Routing effect (SearchPipeline._personalize): a ``deep`` classification
upgrades the request to full fan-out (``deep=True``) even when the caller
did not ask for it — Adaptive-RAG style. ``simple`` keeps the standard
path (we do not shrink the engine set: recall matters more than the
saved milliseconds at our scale).
"""

from __future__ import annotations

import re

# Multi-hop / comparative / analytical markers (VI + EN). A query hitting
# any of these is treated as research-grade and routed to deep fan-out.
_DEEP_MARKERS: tuple[str, ...] = (
    # comparative
    "so sánh",
    "so sanh",
    "khác nhau",
    "khác gì",
    "khác biệt",
    "compare",
    "comparison",
    "versus",
    " vs ",
    " vs. ",
    "đối chiếu",
    # analytical / causal
    "phân tích",
    "phan tich",
    "nguyên nhân",
    "vì sao",
    "lý do",
    "cơ chế",
    "co che",
    "cách hoạt động",
    "how does",
    "why does",
    "why is",
    "what causes",
    "root cause",
    "impact of",
    "affect",
    "mối quan hệ",
    "liên quan",
    "hệ quả",
    "rủi ro",
    "risk of",
    # research-grade
    "nghiên cứu",
    "research",
    "tổng quan",
    "tong quan",
    "đánh giá",
    "danh gia",
    "review",
    "survey",
    "timeline",
    "lịch sử",
    "lich su",
    "history of",
    "evolution of",
    "state of the art",
    "sota",
    "best practices",
    "điểm mạnh yếu",
    "ưu nhược điểm",
    "pros and cons",
    "trade-off",
    "tradeoff",
)

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def classify_complexity(query: str, *, min_words_deep: int = 14) -> str:
    """Return "simple" | "standard" | "deep" for ``query`` (heuristic only)."""
    q = f" {query.lower()} "
    if any(marker in q for marker in _DEEP_MARKERS):
        return "deep"
    words = len(_WORD_RE.findall(q))
    if words >= max(min_words_deep, 4):
        return "deep"
    if words <= 3:
        return "simple"
    return "standard"
