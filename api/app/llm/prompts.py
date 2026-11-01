"""Citation prompt (docs/03-search-pipeline.md stage 11) and context builder."""

from __future__ import annotations

from collections.abc import Callable

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


def build_context(
    chunks: list[Chunk],
    *,
    budget: int,
    count: Callable[[str], int] | None = None,
) -> list[tuple[int, Chunk]]:
    """Pack chunks into numbered context blocks within ``budget`` units.

    ``count`` maps text to the budget unit — words (default proxy) or real
    tokens via ``app.llm.tokens``. Returns the ``(n, chunk)`` pairs actually
    included, numbered from 1, in the order given (caller pre-sorts by
    relevance). The first chunk is always included so the context is never
    empty.
    """
    count = count or (lambda text: len(text.split()))
    included: list[tuple[int, Chunk]] = []
    used = 0
    for n, chunk in enumerate(chunks, start=1):
        size = count(chunk.content) or chunk.word_count
        if used + size > budget and included:
            break
        included.append((n, chunk))
        used += size
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


def build_domain_system_prompt(
    base_prompt: str,
    query: str,
    requested_domain: str = "auto",
) -> str:
    """Enrich the base system prompt with specialized domain forensic guidelines and rubrics."""
    from app.retrieval.domain_intelligence import classify_domain_intelligence

    pack = classify_domain_intelligence(query, requested_domain=requested_domain)
    rubric_text = "\n".join(f"- {r}" for r in pack.verification_rubric)
    dimensions_text = "\n".join(f"- {d}" for d in pack.essence_dimensions)
    return (
        f"{base_prompt}\n\n"
        f"Domain Specialization: {pack.name} (Domain ID: {pack.id}, Maturity: {pack.maturity})\n"
        f"Domain Essence Dimensions:\n{dimensions_text}\n\n"
        f"Verification Rubric:\n{rubric_text}"
    )
