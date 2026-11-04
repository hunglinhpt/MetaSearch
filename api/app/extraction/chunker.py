"""Heading-aware markdown chunker (docs/03-search-pipeline.md stage 9).

Chunks target 512 tokens with 64-token overlap; words are used as the token
proxy so no tokenizer model is loaded on the 16GB dev box. Each chunk carries
its heading path (``H1 > H2``) and position so citations can point back.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

TARGET_WORDS = 512
OVERLAP_WORDS = 64

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(slots=True)
class Chunk:
    id: str
    document_url: str
    position: int  # 1-based within the document
    heading_path: str
    content: str
    word_count: int
    source_title: str = ""


def _new_chunk(
    document_url: str, position: int, heading_path: str, content: str, source_title: str = ""
) -> Chunk:
    words = content.split()
    # FIX1 R1.3: seed uses canonicalized URL (tracking params stripped) +
    # position + content hash. Tracking URL and clean URL → same chunk ID.
    from app.retrieval.canonical import canonicalize

    canon = canonicalize(document_url)
    content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]  # nosec B324
    chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{canon}:{position}:{content_hash}"))
    return Chunk(
        id=chunk_id,
        document_url=document_url,
        position=position,
        heading_path=heading_path,
        content=content,
        word_count=len(words),
        source_title=source_title,
    )


def chunk_markdown(markdown: str, *, document_url: str, source_title: str) -> list[Chunk]:
    """Split ``markdown`` into overlapping chunks, tracking heading paths."""
    chunks: list[Chunk] = []
    position = 0
    for heading_path, body in _split_sections(markdown):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        position = _pack_section(
            paragraphs, heading_path, document_url, source_title, chunks, position
        )

    # An empty document still yields nothing; the caller falls back to the page
    # title so the answer at least cites that the source existed.
    if not chunks and source_title.strip():
        chunks.append(
            _new_chunk(
                document_url,
                position + 1,
                heading_path="",
                content=source_title.strip(),
                source_title=source_title.strip(),
            )
        )
    return chunks


def _pack_section(
    paragraphs: list[str],
    heading_path: str,
    document_url: str,
    source_title: str,
    chunks: list[Chunk],
    position: int,
) -> int:
    buffer: list[str] = []
    buffer_words = 0

    def flush() -> int:
        nonlocal buffer, buffer_words
        if buffer:
            position_local = position
            content = "\n\n".join(buffer).strip()
            if content:
                chunks.append(
                    _new_chunk(
                        document_url, position_local + 1, heading_path, content, source_title
                    )
                )
                position_local += 1
            buffer = []
            buffer_words = 0
            return position_local
        return position

    for para in paragraphs:
        para_words = para.split()
        if len(para_words) > TARGET_WORDS:
            position = flush()
            for start in range(0, len(para_words), TARGET_WORDS - OVERLAP_WORDS):
                window = para_words[start : start + TARGET_WORDS]
                chunks.append(
                    _new_chunk(
                        document_url,
                        position + 1,
                        heading_path,
                        " ".join(window),
                        source_title,
                    )
                )
                position += 1
            continue

        if buffer and buffer_words + len(para_words) > TARGET_WORDS:
            keep = min(OVERLAP_WORDS, buffer_words)
            tail = " ".join(" ".join(buffer).split()[-keep:]) if keep else ""
            position = flush()
            if tail:
                buffer.append(tail)
                buffer_words = len(tail.split())
        buffer.append(para)
        buffer_words += len(para_words)

    return flush()


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Group lines into (heading_path, body) sections."""
    stack: list[tuple[int, str]] = []  # (level, title)
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in markdown.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            sections.append((_path(stack), []))
        else:
            sections[-1][1].append(line)
    return [(path, "\n".join(lines)) for path, lines in sections]


def _path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack if title)
