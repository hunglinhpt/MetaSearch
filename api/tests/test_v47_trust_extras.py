"""docs/36 §18 XERJ-inspired extras: memory dedup (X3), answer grounding (X1),
passage offsets (X2)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.retrieval.memory import is_near_duplicate_text, record_memory


@pytest.fixture
async def mem_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    get_settings.cache_clear()
    from app.db.bootstrap import ensure_schema
    from app.db.session import dispose_engine

    await dispose_engine()
    await ensure_schema()
    yield
    await dispose_engine()
    get_settings.cache_clear()


async def _count(owner: str, kind: str) -> int:
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(MemoryItem)
                .where(MemoryItem.api_key_id == owner, MemoryItem.kind == kind)
            )
        ).scalar_one()


def test_near_duplicate_text():
    assert is_near_duplicate_text("Lãi suất  ngân hàng", "lai suat ngan hang")
    long_a = "Vietcombank giảm lãi suất tiết kiệm kỳ hạn 12 tháng xuống 4,6 phần trăm năm"
    long_b = long_a + " nay"
    assert is_near_duplicate_text(long_a, long_b)  # Jaccard 13/14
    assert not is_near_duplicate_text("giá vàng", "giá bạc")  # short: exact only
    assert not is_near_duplicate_text(long_a, "BIDV tăng lãi suất cho vay mua nhà kỳ hạn 5 năm")


async def test_record_memory_skips_recent_duplicates(mem_db):
    assert await record_memory("k1", kind="query", text="giá vàng hôm nay")
    assert await record_memory("k1", kind="query", text="Giá  vàng hôm nay")  # deduped, still ok
    assert await _count("k1", "query") == 1
    assert await record_memory("k2", kind="query", text="giá vàng hôm nay")  # other owner
    assert await _count("k2", "query") == 1
    # feedback rows are votes: never deduplicated
    for _ in range(2):
        assert await record_memory("k1", kind="feedback", text="", url="https://a.vn", weight=1.0)
    assert await _count("k1", "feedback") == 2


# --- X1: grounding status ------------------------------------------------------------


def _claims(flagged: int = 0, fid: float = 1.0, unverified: list[str] | None = None):
    from app.llm.citations import ClaimCheck

    return ClaimCheck(
        unsupported_markers=[1] if flagged else [],
        flagged_sentences=["x"] * flagged,
        numeric_fidelity=fid,
        unverified_numbers=unverified or [],
    )


def test_grounding_status_ladder():
    from app.llm.citations import grounding_summary

    text = (
        "# Tiêu đề\n"
        "PostgreSQL dùng MVCC để cô lập giao dịch [1]. "
        "Mỗi hàng có xmin và xmax [2]. VACUUM dọn phiên bản cũ [1]."
    )
    g = grounding_summary(text, _claims())
    assert g["status"] == "grounded" and g["cited_sentences"] == 3 and g["prose_sentences"] == 3
    assert grounding_summary(text, _claims(flagged=1))["status"] == "partial"
    assert grounding_summary(text, _claims(flagged=2))["status"] == "weak"
    assert grounding_summary(text, _claims(fid=0.3))["status"] == "weak"
    assert grounding_summary(text, _claims(unverified=["95%"]))["status"] == "partial"
    plain = "PostgreSQL dùng MVCC để cô lập giao dịch. Mỗi hàng có xmin và xmax."
    g = grounding_summary(plain, _claims())
    assert g["status"] == "ungrounded" and g["citation_coverage"] == 0.0
    mostly_uncited = (
        text + " Câu này không có nguồn. Câu kia cũng vậy thôi. Thêm một câu nữa. Và câu cuối cùng."
    )
    assert grounding_summary(mostly_uncited, _claims())["status"] == "partial"


# --- X2: passage offsets --------------------------------------------------------------


def test_chunk_offsets_point_back_into_the_document():
    from app.extraction.chunker import chunk_markdown

    para = " ".join(f"từ{i}" for i in range(700))  # long: windowed with overlap
    md = f"# Đầu\n\nĐoạn mở đầu ngắn gọn.\n\n## Phần hai\n\n{para}\n\nĐoạn   kết   thúc."
    chunks = chunk_markdown(md, document_url="https://x.vn/a", source_title="T")
    assert len(chunks) >= 3
    prev = -1
    for c in chunks:
        assert c.start_offset is not None and c.end_offset is not None
        span = md[c.start_offset : c.end_offset]
        assert " ".join(span.split()) == " ".join(c.content.split())
        assert c.start_offset >= prev  # document order, overlap allowed
        prev = c.start_offset


def test_pdf_pages_and_unlocatable_chunks():
    from app.extraction.chunker import Chunk, assign_offsets, chunk_markdown

    md = "## p.1\n\nTrang một nói về MVCC.\n\n## p.2\n\nTrang hai nói về VACUUM."
    chunks = chunk_markdown(md, document_url="https://x.vn/a.pdf", source_title="T")
    assert [c.page for c in chunks] == [1, 2]
    ghost = Chunk(
        id="g",
        document_url="u",
        position=1,
        heading_path="",
        content="không có ở đây",
        word_count=3,
    )
    assign_offsets(md, [ghost])
    assert ghost.start_offset is None and ghost.end_offset is None and ghost.page is None
    # title fallback is not a document span
    (fallback,) = chunk_markdown("", document_url="https://x.vn/e", source_title="Tiêu đề")
    assert fallback.start_offset is None


def test_offsets_stay_linear_on_large_documents():
    import time

    from app.extraction.chunker import Chunk, assign_offsets, chunk_markdown

    md = "\n\n".join(f"Đoạn số {i} nói về chủ đề {i % 97} với vài từ đệm." for i in range(20000))
    chunks = chunk_markdown(md, document_url="https://x.vn/big", source_title="T")
    ghosts = [
        Chunk(
            id=f"g{i}",
            document_url="u",
            position=i,
            heading_path="",
            content=f"không tồn tại {i} " * 20,
            word_count=60,
        )
        for i in range(300)
    ]
    t = time.perf_counter()
    assign_offsets(md, ghosts + chunks)
    assert time.perf_counter() - t < 3.0  # ~1 MB doc, 300 unlocatable anchors
    assert all(g.start_offset is None for g in ghosts)


def test_polymarket_naive_timestamps_become_utc():
    from app.retrieval.adapters.polymarket import _parse_dt

    assert _parse_dt("2026-11-01T05:07:00").tzinfo is not None
    assert _parse_dt("2026-11-01T05:07:00Z").utcoffset().total_seconds() == 0
    assert _parse_dt("not a date") is None and _parse_dt(None) is None
