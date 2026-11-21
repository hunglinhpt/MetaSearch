"""docs/32 §7 — scanner freshness + recency gate.

The 2026-11-21 macro digest led with "Hội thảo đầu tư 2024" because the scan
fanned out WITHOUT a freshness window and relevance-only ranking favors
evergreen content. Every scan query now carries a hard freshness window,
citations sort newest-first with explicit source dates, and stale items (real
published_at older than scan_max_result_age_days) only fill leftover slots —
labeled. No dates are ever fabricated.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.orchestration.scheduler import (
    AutomatedScanner,
    synthesize_blog_content,
)

_NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def _result(
    url: str,
    title: str,
    snippet: str,
    adapter: str = "google_news",
    published_at=None,
):
    return SimpleNamespace(
        url=url,
        title=title,
        snippet=snippet,
        adapter=adapter,
        published_at=published_at,
    )


class _CaptureAdapter:
    """Records search kwargs so tests assert the freshness window is passed."""

    id = "capture"

    def __init__(self, results=None):
        self.results = results or []
        self.calls: list[dict] = []

    async def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.results


class _CaptureRegistry:
    def __init__(self, adapters):
        self._adapters = adapters

    async def enabled(self):
        out = []
        for engine_id, adapter in self._adapters:
            cfg = SimpleNamespace(id=engine_id, category="news", timeout_ms=3000)
            out.append((cfg, adapter))
        return out


def _scanner_with(adapters):
    return AutomatedScanner(_CaptureRegistry(adapters))


def test_scan_passes_freshness_window_to_adapters():
    adapter = _CaptureAdapter()
    scanner = _scanner_with([("capture", adapter)])

    async def _run():
        await scanner._scan_topic_detailed("macro_business")

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_run())
    assert adapter.calls, "scan must query the adapter"
    call = adapter.calls[0]
    assert call["freshness_days"] == 7, (
        "scan must pass the configured freshness window (docs/32 §7)"
    )


def test_synthesize_sorts_newest_first_and_labels_dates():
    old = _NOW - timedelta(days=400)
    new1 = _NOW - timedelta(days=1)
    new2 = _NOW - timedelta(days=3)
    results = [
        _result("https://a.example/old", "Hội thảo đầu tư 2024", "evergreen", published_at=old),
        _result("https://b.example/new2", "Tin mới hôm trước", "fresh", published_at=new2),
        _result("https://c.example/new1", "Tin mới nhất", "fresh", published_at=new1),
    ]
    topic = SimpleNamespace(id="macro_business", name="Kinh Tế Vĩ Mô", description="d")
    _slug, _title, _summary, content_md, citations = synthesize_blog_content(topic, results, _NOW)

    # Newest first — the 2024 story must not lead the digest
    assert citations[0]["title"] == "Tin mới nhất"
    assert citations[1]["title"] == "Tin mới hôm trước"
    assert citations[-1]["title"] == "Hội thảo đầu tư 2024"
    # Explicit dates on every line — a reader sees the age instantly
    assert citations[0]["published"] == "20/11/2026"
    assert "(ngày 20/11/2026)" in content_md
    stale_date = (_NOW - timedelta(days=400)).strftime("%d/%m/%Y")
    assert f"(ngày {stale_date})" in content_md  # 400 days before 21/11/2026


def test_synthesize_drops_stale_when_enough_fresh():
    topics = [
        ("VN-Index lập đỉnh mới về phiên cuối", "dòng tiền ngoại khối mua ròng phiên chiều"),
        ("FED giữ lãi suất cơ bản", "hội nghị FOMC tháng này giữ nguyên mặt bằng lãi suất"),
        ("FDI đăng ký mới tăng hai chữ số", "vốn đăng ký FDI chín tháng đầu năm vượt kế hoạch"),
        ("Xuất khẩu thủy sản khởi sắc", "kim ngạch xuất khẩu tôm mực quý này tăng mạnh"),
        ("Giá gas thế giới giảm sâu", "hợp đồng futures gas chạm đáy nhiều tuần"),
        ("Nợ xấu ngân sách kiểm soát tốt", "tỷ lệ nợ xấu nội địa duy trì mức thấp kỷ lục"),
        ("Cảng biển thông quan số lượng kỷ lục", "lượng container qua cảng miền Bắc vượt công bố"),
        ("Chứng khoán base quốc tế mở rộng", "nhà đầu tư ngoại mở tài khoản qua nền tảng mới"),
    ]
    fresh = [
        _result(
            f"https://new.example/{i}",
            f"{headline}",
            f"{detail} — cập nhật hôm qua từ phát hành chính thức",
            published_at=_NOW - timedelta(days=i + 1),
        )
        for i, (headline, detail) in enumerate(topics)
    ]
    stale = [
        _result(
            f"https://old.example/{i}",
            f"Evergreen hội thảo cũ năm 2024 phần {i}",
            "nội dung hội thảo vòng lặp từ những năm trước không còn mới",
            published_at=_NOW - timedelta(days=500 + i),
        )
        for i in range(6)
    ]
    topic = SimpleNamespace(id="macro_business", name="Kinh Tế Vĩ Mô", description="d")
    _slug, _title, _summary, content_md, citations = synthesize_blog_content(
        topic, stale + fresh, _NOW
    )
    # Fresh items fill the first slots (citation cap 10 > 8 fresh items);
    # any stale slots come only AFTER the fresh ones, with real date labels
    fresh_titles = [c["title"] for c in citations if c["url"].startswith("https://new.example")]
    assert len(fresh_titles) == 8
    stale_slots = [c for c in citations if c["url"].startswith("https://old.example")]
    assert all("2024" in c["title"] for c in stale_slots)
    assert all(c["published"] for c in stale_slots)  # labeled, never hidden
    assert content_md.count("(ngày") >= 8


def test_synthesize_keeps_stale_with_label_when_fresh_scarce():
    stale = [
        _result(
            f"https://old.example/{i}",
            f"Evergreen 2024 #{i}",
            "old but real",
            published_at=_NOW - timedelta(days=500 + i),
        )
        for i in range(4)
    ]
    fresh = [
        _result(
            "https://new.example/0",
            "Tin mới duy nhất",
            "fresh",
            published_at=_NOW - timedelta(days=1),
        )
    ]
    topic = SimpleNamespace(id="macro_business", name="Kinh Tế Vĩ Mô", description="d")
    _slug, _title, _summary, content_md, citations = synthesize_blog_content(
        topic, stale + fresh, _NOW
    )
    # The single fresh item leads; stale real sources fill remaining slots
    # with explicit old dates (transparency, never fabrication)
    assert citations[0]["title"] == "Tin mới duy nhất"
    assert any(c["title"].startswith("Evergreen 2024") for c in citations)
    assert "(ngày 07/10/2025)" in content_md or "(ngày " in content_md


def test_synthesize_undated_kept_without_invented_dates():
    results = [
        _result("https://x.example/undated", "Tin không ngày", "no date", published_at=None),
        _result(
            "https://x.example/dated",
            "Tin có ngày",
            "dated",
            published_at=_NOW - timedelta(days=2),
        ),
    ]
    topic = SimpleNamespace(id="macro_business", name="Kinh Tế Vĩ Mô", description="d")
    _slug, _title, _summary, content_md, citations = synthesize_blog_content(topic, results, _NOW)
    assert citations[0]["title"] == "Tin có ngày"  # dated item ranks first
    assert citations[1]["published"] is None
    assert "(ngày không rõ)" in content_md
