"""Citation prompt (docs/03-search-pipeline.md stage 11) and context builder."""

from __future__ import annotations

from app.extraction.chunker import Chunk

SYSTEM_PROMPT = """You are MetaSearch, a meticulous research assistant. You answer user \
questions using ONLY the numbered web sources provided in the context.

Rules:
1. Every factual claim MUST carry a citation marker like [1] or [2] referring to the \
numbered source it comes from. A sentence drawing on two sources uses [1][2].
2. Never invent facts, sources, or citation numbers. If the context does not contain \
the answer, say plainly that you could not find it.
3. Answer in the SAME LANGUAGE as the user's question (Vietnamese question → \
Vietnamese answer).
4. Prefer synthesizing across sources over repeating one source. Be concise but complete.
5. Do not cite the same source number after every sentence; cite where the claim is made.
6. You may use markdown for structure (short paragraphs, bullet lists)."""


def build_context(chunks: list[Chunk], *, budget_words: int) -> list[tuple[int, Chunk]]:
    """Pack chunks into numbered context blocks within ``budget_words``.

    Returns the ``(n, chunk)`` pairs actually included, numbered from 1, in the
    order given (caller is responsible for pre-sorting by relevance).
    """
    included: list[tuple[int, Chunk]] = []
    used = 0
    for n, chunk in enumerate(chunks, start=1):
        words = chunk.word_count or len(chunk.content.split())
        if used + words > budget_words and included:
            break
        included.append((n, chunk))
        used += words
    return included


def render_context(included: list[tuple[int, Chunk]]) -> str:
    """Render numbered sources for the prompt, e.g. ``[1] Title — URL``."""
    blocks: list[str] = []
    for n, chunk in included:
        header = f"[{n}] {chunk.document_url}"
        if chunk.heading_path:
            header += f" (section: {chunk.heading_path})"
        blocks.append(f"{header}\n{chunk.content}")
    return "\n\n".join(blocks)


def build_messages(question: str, included: list[tuple[int, Chunk]]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Sources:\n\n{render_context(included)}\n\nQuestion: {question}",
        },
    ]
