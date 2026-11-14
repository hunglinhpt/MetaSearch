"""RAGAS-lite LLM-as-judge for the eval harness (docs/23 P1 #2).

Scores the two RAG Triad legs the lexical proxy cannot measure:
- faithfulness: fraction of the answer's factual claims grounded in the cited
  sources (TruLens "groundedness");
- answer relevance: how directly the answer addresses the question.

One cheap LLM completion per answer; scores in [0, 1]; fail-open returns
None so the eval harness keeps running without an LLM endpoint (AGENTS §4).
"""

from __future__ import annotations

import asyncio
import json as _json
import re as _re
from typing import Any

_MAX_PROMPT_CHARS = 8000
_MAX_SOURCES = 6
_MAX_SOURCE_CHARS = 600
_MAX_ANSWER_CHARS = 4000


def _build_prompt(question: str, sources: list[str], answer: str) -> str:
    src_block = "\n".join(s[:_MAX_SOURCE_CHARS] for s in sources[:_MAX_SOURCES])
    prompt = (
        "You are an evaluation judge. Score the ANSWER on two axes, "
        "each a float between 0.0 and 1.0.\n"
        "1. faithfulness: fraction of the answer's factual claims that are "
        "supported by the SOURCES (0 = nothing supported, 1 = all supported).\n"
        "2. answer_relevance: how directly the answer addresses the QUESTION.\n"
        'Reply with ONLY a JSON object: {"faithfulness": <float>, '
        '"answer_relevance": <float>}\n\n'
        f"QUESTION: {question}\n\n"
        f"SOURCES (truncated):\n{src_block}\n\n"
        f"ANSWER (truncated): {answer[:_MAX_ANSWER_CHARS]}"
    )
    return prompt[:_MAX_PROMPT_CHARS]


def parse_judge_scores(text: str) -> dict[str, float | None]:
    """Extract the two judge scores from the LLM's reply; None when absent
    or out of range (never fabricated)."""
    match = _re.search(r"\{[^{}]*faithfulness[^{}]*\}", text, _re.DOTALL)
    if not match:
        return {"faithfulness": None, "answer_relevance": None}
    try:
        parsed = _json.loads(match.group(0))
    except _json.JSONDecodeError:
        return {"faithfulness": None, "answer_relevance": None}
    out: dict[str, float | None] = {}
    for key in ("faithfulness", "answer_relevance"):
        val = parsed.get(key)
        if isinstance(val, (int, float)) and 0.0 <= float(val) <= 1.0:
            out[key] = round(float(val), 3)
        else:
            out[key] = None
    return out


async def llm_judge(
    gateway: Any,
    answer_md: str,
    question: str,
    sources: list[str],
    *,
    model: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, float | None]:
    """Score faithfulness + answer relevance via one LLM-as-judge call.

    Returns ``{"faithfulness": float|None, "answer_relevance": float|None}``
    — None values mean the judge could not score (fail-open, never raises).
    """
    stripped = answer_md.replace("```", " ").strip()
    if not stripped or not sources:
        return {"faithfulness": None, "answer_relevance": None}

    prompt = _build_prompt(question, sources, stripped)

    async def _score() -> dict[str, float | None]:
        kwargs: dict[str, Any] = {"task": "ragas_judge"}
        if model:
            kwargs["model"] = model
        result = await asyncio.wait_for(
            gateway.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=120,
                **kwargs,
            ),
            timeout=timeout_s,
        )
        text = result[0] if isinstance(result, tuple) else str(result)
        return parse_judge_scores(text)

    try:
        return await asyncio.wait_for(_score(), timeout=timeout_s + 5.0)
    except (TimeoutError, Exception):
        return {"faithfulness": None, "answer_relevance": None}
