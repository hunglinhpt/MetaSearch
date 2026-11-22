"""Scheduler pure-helper coverage round 2 (docs/29 P0-1/P1-3/PASS2): markdown
escaping, result dedup + simhash syndication fold, honest delta section."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.db.models import BlogPost
from app.orchestration.scheduler import (
    _build_delta_section,
    _dedup_results,
    _md_escape,
    _safe_link_url,
    _sources_block,
)


def _res(url: str, title: str, snippet: str = "nội dung", adapter: str = "google_news", **extra):
    return SimpleNamespace(
        url=url, title=title, snippet=snippet, adapter=adapter, published_at=None, **extra
    )


def test_md_escape_neutralizes_structural_chars():
    raw = "Tiêu đề ](https://evil.example *nhấn mạnh* #tag <script>"
    out = _md_escape(raw, limit=400)
    for ch in ("[", "]", "(", ")", "*", "`", "<", ">", "#"):
        assert ch not in out
    # collapsed whitespace + capped
    assert "\n" not in _md_escape("a\nb\rc", limit=400)
    assert len(_md_escape("x" * 500)) == 220


def test_dedup_results_skips_urlless_and_folds_syndication():
    res = [
        _res("https://a.example/story", "Câu chuyện A", "mô tả dài nhất trong các bản tin"),
        _res(
            "https://a.example/story?utm_source=x",
            "Câu chuyện A (bản Erica)",
            "ngắn",
            adapter="bing_news",
        ),
        # near-identical story syndicated on a different URL (simhash fold)
        _res(
            "https://b.example/syndicated",
            "Câu chuyện A",
            "mô tả dài nhất trong các bản tin",
            adapter="vn_wire",
        ),
        _res("", "không có url", "bị bỏ qua"),
    ]
    out = _dedup_results(res)
    assert len(out) == 1
    assert set(out[0]["engines"]) == {"google_news", "bing_news", "vn_wire"}
    # richest snippet wins on the canonical entry
    assert "dài nhất" in out[0]["snippet"]
    assert out[0]["title"] == "Câu chuyện A"
    assert out[0]["hash"]


def test_dedup_results_invalid_url_falls_back_to_raw():
    out = _dedup_results([_res("http://[invalid", "T")])
    assert len(out) == 1 and out[0]["url"] == "http://[invalid"


def _post_with_sources(urls: list[str], published: datetime) -> BlogPost:
    return BlogPost(
        title="kỳ trước",
        summary="s",
        slug="kỳ-trước",
        topic="t",
        sources=[{"url": u} for u in urls],
        published_at=published,
    )


def test_delta_section_no_previous_post_is_baseline():
    out = _build_delta_section(None, [{"title": "T", "url": "https://a", "snippet": "s"}])
    assert "Bối Cảnh Khởi Điểm" in out


def test_delta_section_fresh_sources_listed_with_escaped_dates():
    prev = _post_with_sources(["https://old.example/a"], datetime.now(UTC) - timedelta(days=1))
    # titles arrive already escaped by _dedup_results in the real flow
    fresh = [
        {"title": "Tin mới (hot)", "url": "https://new.example/x", "snippet": "s"},
    ]
    out = _build_delta_section(prev, fresh)
    assert "Nguồn Mới So Với Kỳ" in out
    assert "1 nguồn" in out
    assert "Tin mới" in out


def test_delta_section_canonical_compare_no_false_fresh():
    prev = _post_with_sources(["https://www.old.example/a?utm_campaign=x"], datetime.now(UTC))
    same_story = [{"title": "T", "url": "https://old.example/a", "snippet": "s"}]
    out = _build_delta_section(prev, same_story)
    assert "Không ghi nhận nguồn mới" in out


def test_delta_section_unparseable_previous_urls_do_not_crash():
    prev = _post_with_sources(["http://[bad"], datetime.now(UTC))
    out = _build_delta_section(
        prev, [{"title": "T", "url": "https://new.example/z", "snippet": "s"}]
    )
    assert "Nguồn Mới" in out


def test_sources_block_and_safe_link_url():
    url = "https://vi.wikipedia.org/wiki/Python_(ngôn_ngữ_lập_trình)"
    safe = _safe_link_url(url)
    assert "(" not in safe and ")" not in safe
    block = _sources_block(
        [
            {
                "n": 1,
                "url": "https://a.example/1",
                "title": "Nguồn A",
                "snippet": "s",
                "engines": ["google_news"],
            },
            {
                "n": 2,
                "url": "https://b.example/2",
                "title": "Nguồn B",
                "snippet": "x" * 200,
                "engines": [],
            },
        ]
    )
    assert "[1] [Nguồn A]" in block and "engine: google_news" in block
    assert "(…)" in block or "…" in block  # long snippet ellipsis
    assert _sources_block([]) == "- Không thu được nguồn tin hợp lệ trong kỳ này."
