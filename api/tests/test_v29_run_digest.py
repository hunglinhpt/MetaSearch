"""docs/29 P0-1: scanner integrity + unified run digest ("Bản tin chung").

Extractive synthesis must never publish fabricated claims or placeholder
sources; dedup folds canonical-URL duplicates; the unified digest aggregates
the whole run with global citation numbering. Scan runs are single-flight
and parallel-bounded.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.orchestration.scheduler import (
    DEFAULT_TOPICS,
    RUN_DIGEST_TOPIC,
    AutomatedScanner,
    _dedup_results,
    _md_escape,
    _ScanOutcome,
    synthesize_blog_content,
    synthesize_run_digest,
)


def _result(url: str, title: str, snippet: str, adapter: str = "searxng"):
    return SimpleNamespace(url=url, title=title, snippet=snippet, adapter=adapter)


NOW = datetime.now(UTC)


class TestMdEscape:
    def test_neutralizes_markdown_structural_chars(self):
        out = _md_escape("Title ](http://evil) with *bold* and [link]")
        assert "]" not in out and "[" not in out and "(" not in out and "*" not in out

    def test_flattens_newlines(self):
        assert "\n" not in _md_escape("a\nb\rc")


class TestDedupResults:
    def test_folds_same_canonical_url_across_engines(self):
        results = [
            _result(
                "https://vnexpress.net/a?utm_source=x",
                "Tin A",
                "nội dung A",
                "searxng",
            ),
            _result(
                "https://www.vnexpress.net/a",
                "Tin A",
                "nội dung A dài hơn chút",
                "google_news",
            ),
        ]
        out = _dedup_results(results)
        assert len(out) == 1
        assert set(out[0]["engines"]) == {"searxng", "google_news"}
        # richest snippet wins
        assert "dài hơn" in out[0]["snippet"]

    def test_skips_results_without_url(self):
        out = _dedup_results([_result("", "No URL", "body")])
        assert out == []

    def test_near_duplicate_story_folds_across_urls(self):
        text = "Cua hóa VN-Index tăng điểm phiên sáng thanh khoản lớn"
        results = [
            _result("https://site-a.com/x", text, text),
            _result("https://site-b.com/mirror", text, text),
        ]
        out = _dedup_results(results)
        assert len(out) == 1

    def test_distinct_stories_stay_separate(self):
        out = _dedup_results(
            [
                _result("https://a.com/1", "Chính sách lãi suất mới", "Fed giữ lãi suất"),
                _result("https://b.com/2", "Sản xuất thép tăng", "HPG công bố sản lượng"),
            ]
        )
        assert len(out) == 2


class TestExtractiveSynthesis:
    def test_no_fabricated_claims_when_no_results(self):
        topic = DEFAULT_TOPICS["viet_finance"]
        _slug, _title, _summary, content_md, citations = synthesize_blog_content(topic, [], NOW)
        # The fabricated-template era: these claims were hardcoded. Now absent.
        assert "định giá hấp dẫn" not in content_md
        assert "Dòng tiền thông minh" not in content_md
        assert "Không có tin hợp lệ" in content_md
        assert citations == []

    def test_content_comes_from_real_results(self):
        topic = DEFAULT_TOPICS["viet_finance"]
        results = [
            _result(
                "https://cafef.vn/vn-index-tang",
                "VN-Index tăng 15 điểm phiên sáng",
                "Thanh khoản HOSE đạt 25 nghìn tỷ đồng",
            )
        ]
        (
            _slug,
            _title,
            _summary,
            content_md,
            citations,
        ) = synthesize_blog_content(topic, results, NOW)
        assert "VN-Index tăng 15 điểm" in content_md  # real title present
        assert "Thanh khoản HOSE" in content_md  # real snippet present
        assert citations and citations[0]["url"] == "https://cafef.vn/vn-index-tang"
        assert "[Nguồn 1]" in content_md

    def test_never_fabricates_a_placeholder_source(self):
        topic = DEFAULT_TOPICS["ai_coding"]
        results = [
            _result("https://x.dev/post", "Real post", "body"),
            _result("", "no url", "b"),
        ]
        _slug, _t, _s, content_md, citations = synthesize_blog_content(topic, results, NOW)
        assert "https://metasearch.ai" not in content_md  # old placeholder is gone
        assert all(c["url"] == "https://x.dev/post" for c in citations)

    def test_escaping_prevents_link_hijack(self):
        topic = DEFAULT_TOPICS["macro_business"]
        results = [
            _result(
                "https://ok.example/news",
                "Vấn đề ](http://evil.example) nghiêm trọng",
                "nội dung",
            )
        ]
        _slug, _t, _s, content_md, citations = synthesize_blog_content(topic, results, NOW)
        # the markdown link-hijack pattern ]( must not survive; the raw URL
        # text itself may remain inside the (escaped) title
        assert "](http://evil" not in content_md
        assert citations[0]["title"] == "Vấn đề http://evil.example nghiêm trọng"

    def test_honest_delta_when_no_new_sources(self):
        topic = DEFAULT_TOPICS["macro_business"]
        results = [_result("https://ok.example/news", "Tin cũ", "nội dung")]
        _slug, _t, _s, content_md, _c1 = synthesize_blog_content(topic, results, NOW)
        prev = SimpleNamespace(
            published_at=NOW - timedelta(days=1),
            sources=[{"n": 1, "title": "Tin cũ", "url": "https://ok.example/news"}],
        )
        _s2, _t2, _s3, content_md2, _c2 = synthesize_blog_content(
            topic, results, NOW, previous_post=prev
        )
        assert "Không ghi nhận nguồn mới nào" in content_md2
        assert content_md2 != content_md  # delta section actually changed


class TestUnifiedRunDigest:
    def _outcome(
        self,
        topic_id: str,
        urls: list[str],
        engines_ok: int = 2,
        engines_failed: int = 0,
        error: str | None = None,
    ) -> _ScanOutcome:
        topic = DEFAULT_TOPICS[topic_id]
        results = [
            _result(u, f"Tiêu đề {topic_id} {i}", f"Nội dung thật {topic_id} {i}", "searxng")
            for i, u in enumerate(urls)
        ]
        post = None
        if error is None:
            citations = [
                {
                    "n": i + 1,
                    "title": f"Tiêu đề {topic_id} {i}",
                    "url": u,
                    "snippet": "nội dung",
                    "engines": ["searxng"],
                }
                for i, u in enumerate(urls)
            ]
            post = SimpleNamespace(
                id="x",
                slug=f"{topic_id}-s",
                topic=topic.id,
                title=topic.name,
                summary="",
                content_md="",
                tags=[],
                sources=citations,
                published_at=NOW,
                created_at=NOW,
            )
        return _ScanOutcome(
            topic=topic,
            post=post,
            citations=[] if post is None else citations,
            raw_count=len(results),
            deduped_count=len(urls),
            engines_ok=engines_ok,
            engines_failed=engines_failed,
            error=error,
        )

    def test_digest_aggregates_all_topics(self):
        outcomes = [
            self._outcome("viet_finance", ["https://a.com/vn1", "https://a.com/vn2"]),
            self._outcome("ai_coding", ["https://b.com/ai1"]),
            self._outcome("health_wellness", [], error="engine down"),
        ]
        slug, title, _summary, content_md, citations = synthesize_run_digest(outcomes, NOW)
        assert slug.startswith("run-digest-")
        assert "Bản Tin Chung" in title
        assert "Thống kê đợt chạy" in content_md
        assert "3 tin thô" in content_md  # raw count printed (2+1+0)
        assert "Lỗi khi quét chủ đề" in content_md  # failed topic reported honestly
        assert "Tiêu đề viet_finance 0" in content_md  # real headline surfaced
        # global citation numbering is contiguous and url-deduped
        assert [c["n"] for c in citations] == list(range(1, len(citations) + 1))
        assert len({c["url"] for c in citations}) == len(citations)

    def test_cross_topic_hot_source_detected(self):
        outcomes = [
            self._outcome("viet_finance", ["https://hot.example/story"]),
            self._outcome("macro_business", ["https://hot.example/story", "https://m.com/x"]),
        ]
        _slug, _t, _s, content_md, citations = synthesize_run_digest(outcomes, NOW)
        assert "Nguồn Nổi Bật Liên Chủ Đề" in content_md
        assert "https://hot.example/story" in content_md
        hot_entry = next(c for c in citations if c["url"] == "https://hot.example/story")
        assert set(hot_entry["topics"]) == {
            "Thị Trường Chứng Khoán & Tài Chính Việt Nam",
            "Kinh Doanh & Kinh Tế Vĩ Mô",
        }

    def test_top_items_prefer_multi_topic_sources(self):
        outcomes = [
            self._outcome("viet_finance", ["https://hot.example/story"]),
            self._outcome("macro_business", ["https://hot.example/story", "https://m.com/x"]),
        ]
        _slug, _t, _s, content_md, _c = synthesize_run_digest(outcomes, NOW)
        # the multi-topic source ranks first in the whole-run top list: its
        # provenance annotation carries BOTH topic names (url dedup means the
        # hot source shares one title, so rank order is proven by topics)
        top_section = content_md.split("## Tin Nổi Bật Toàn Đợt")[1].split("\n## ")[0]
        first_item = next(ln for ln in top_section.splitlines() if ln.strip().startswith("1."))
        assert "(chủ đề: Kinh Doanh" in first_item
        assert "Thị Trường Chứng Khoán" in first_item


class _FakeAdapter:
    def __init__(self, results, fail=False):
        self.results = results
        self.fail = fail
        self.calls = 0

    async def search(self, query, max_results=5, timeout_s=1.0):
        self.calls += 1
        if self.fail:
            raise RuntimeError("engine down")
        return self.results


class TestScanAllBehavior:
    def _scanner(self, adapters):
        registry = SimpleNamespace(
            enabled=lambda: asyncio.sleep(
                0,
                result=[
                    (SimpleNamespace(id=f"e{i}", category="news", timeout_ms=1000), a)
                    for i, a in enumerate(adapters)
                ],
            )
        )
        notifier = SimpleNamespace(
            send_briefing_notification=lambda post: asyncio.sleep(0, result=None)
        )

        async def _notify(post):
            return None

        notifier.send_briefing_notification = _notify
        return AutomatedScanner(registry, notifier=notifier)

    async def test_scan_all_creates_topic_posts_plus_unified_digest(self, monkeypatch, tmp_path):
        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        monkeypatch.setattr(
            get_settings(), "database_url", f"sqlite+aiosqlite:///{tmp_path}/scan.db"
        )
        await dispose_engine()
        await ensure_schema()

        adapters = [
            _FakeAdapter(
                [
                    _result(
                        "https://news.example/x",
                        "Tin thực tế",
                        "Nội dung tin thực tế cho chủ đề",
                        "searxng",
                    ),
                ]
            )
        ]
        scanner = self._scanner(adapters)

        async def fake_enabled():
            return [(SimpleNamespace(id="e0", category="news", timeout_ms=500), adapters[0])]

        scanner.registry.enabled = fake_enabled
        posts = await scanner.scan_all()
        # 6 topic posts + 1 unified digest
        assert len(posts) == len(DEFAULT_TOPICS) + 1
        digest = posts[-1]
        assert digest.topic == RUN_DIGEST_TOPIC
        assert "Bản Tin Chung" in digest.title
        assert all(p.topic != RUN_DIGEST_TOPIC for p in posts[:-1])
        # real content surfaced in the digest
        assert "Tin thực tế" in digest.content_md

    async def test_scan_all_respects_last_run_in_background_mode(self, monkeypatch, tmp_path):
        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        monkeypatch.setattr(
            get_settings(), "database_url", f"sqlite+aiosqlite:///{tmp_path}/scan2.db"
        )
        await dispose_engine()
        await ensure_schema()

        adapter = _FakeAdapter([])
        scanner = self._scanner([adapter])

        async def fake_enabled():
            return [(SimpleNamespace(id="e0", category="news", timeout_ms=500), adapter)]

        scanner.registry.enabled = fake_enabled
        await scanner.scan_all()
        calls_after_first = adapter.calls
        # all topics were just scanned — a background run must skip them all
        posts = await scanner.scan_all(respect_last_run=True, interval_hours=24.0)
        assert posts == []
        assert adapter.calls == calls_after_first
