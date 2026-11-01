"""Tests for Google News RSS, Feng Shui adapter, Research Profiles, Pipeline & Daily Digests API."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.models import BlogPost
from app.main import create_app
from app.orchestration.notifier import NotificationDispatcher
from app.orchestration.research import detect_profile
from app.orchestration.scheduler import (
    DEFAULT_TOPICS,
    _build_delta_section,
    synthesize_blog_content,
)
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.fengshui import FengShuiAdapter, _calc_cung_phi_nam, _calc_cung_phi_nu
from app.retrieval.adapters.google_news import GoogleNewsAdapter
from app.retrieval.models import RawResult


@pytest.fixture
def patched_httpx(monkeypatch):
    original = httpx.AsyncClient

    def _patch(module_name: str, handler):
        module = __import__(module_name, fromlist=["httpx"])

        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(module.httpx, "AsyncClient", make_client)

    return _patch


class TestGoogleNewsAdapter:
    async def test_empty_query(self):
        adapter = GoogleNewsAdapter()
        assert await adapter.search("   ") == []

    async def test_parses_rss_feed(self, patched_httpx):
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>Google News</title>
                <item>
                    <title>VN-Index tăng điểm mạnh phiên cuối tuần - VnExpress</title>
                    <link>https://vnexpress.net/vn-index-tang-diem-4800000.html</link>
                    <description>
                        &lt;a href="..."&gt;Thị trường phục hồi...&lt;/a&gt;
                    </description>
                    <pubDate>Sun, 01 Nov 2026 03:00:00 GMT</pubDate>
                    <source url="https://vnexpress.net">VnExpress</source>
                </item>
            </channel>
        </rss>"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=xml_content)

        patched_httpx("app.retrieval.adapters.google_news", handler)

        adapter = GoogleNewsAdapter()
        # Test both positional and pipeline keyword call
        results = await adapter.search(
            "chứng khoán hôm nay",
            lang="vi",
            categories=["news"],
            max_results=5,
            timeout_s=3.0,
        )
        assert len(results) == 1
        assert "VN-Index tăng điểm" in results[0].title
        assert results[0].url == "https://vnexpress.net/vn-index-tang-diem-4800000.html"
        assert "[VnExpress]" in results[0].snippet
        assert results[0].adapter == "google_news"
        assert results[0].published_at is not None

    async def test_upstream_error_raises_adapter_error(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        patched_httpx("app.retrieval.adapters.google_news", handler)

        adapter = GoogleNewsAdapter()
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("AI news")
        assert "google_news_upstream_error" in str(exc_info.value)


class TestFengShuiAdapter:
    async def test_empty_query(self):
        adapter = FengShuiAdapter()
        assert await adapter.search("   ") == []

    def test_cung_phi_mathematical_precision(self):
        # 1990: Nam Khảm (Đông tứ), Nữ Cấn (Tây tứ)
        assert _calc_cung_phi_nam(1990) == "Khảm"
        assert _calc_cung_phi_nu(1990) == "Cấn"
        # 1989: Nam Khôn (Tây tứ), Nữ Tốn (Đông tứ)
        assert _calc_cung_phi_nam(1989) == "Khôn"
        assert _calc_cung_phi_nu(1989) == "Tốn"
        # 1988: Nam Chấn (Đông tứ), Nữ Chấn (Đông tứ)
        assert _calc_cung_phi_nam(1988) == "Chấn"
        assert _calc_cung_phi_nu(1988) == "Chấn"
        # 2000: Nam Ly (Đông tứ), Nữ Càn (Tây tứ)
        assert _calc_cung_phi_nam(2000) == "Ly"
        assert _calc_cung_phi_nu(2000) == "Càn"

    async def test_calculates_can_chi_and_bat_trach_1990(self):
        adapter = FengShuiAdapter()
        results = await adapter.search(
            "nam sinh năm 1990 hướng nhà phong thủy",
            lang="vi",
            categories=["fengshui"],
            max_results=5,
        )
        assert len(results) >= 1
        top = results[0]
        assert "1990" in top.title
        assert "Canh Ngọ" in top.snippet
        assert "Lộ Bàng Thổ" in top.snippet
        assert "Đông Tứ Mệnh" in top.snippet
        assert "Sinh Khí" in top.snippet
        assert top.adapter == "fengshui"

    async def test_general_principles_query(self):
        adapter = FengShuiAdapter()
        results = await adapter.search("phong thủy bàn làm việc thu hút tài lộc")
        assert len(results) >= 1
        assert any("Tàng phong tụ khí" in r.snippet or "Bàn làm việc" in r.snippet for r in results)
        assert results[0].adapter == "fengshui"


class TestResearchProfileDetection:
    def test_detects_fengshui(self):
        assert detect_profile("hướng nhà phong thủy tuổi 1990") == "fengshui"
        assert detect_profile("bát trạch và ngũ hành nạp âm bàn làm việc") == "fengshui"

    def test_detects_health(self):
        assert detect_profile("triệu chứng và phương pháp điều trị tiểu đường") == "health"
        assert detect_profile("clinical trial therapy for hypertension") == "health"

    def test_detects_news(self):
        assert detect_profile("tin tức thời sự hôm nay mới nhất") == "news"


class TestDigestsAndScanner:
    def test_synthesizes_blog_content(self):
        from datetime import UTC, datetime

        topic = DEFAULT_TOPICS["ai_coding"]
        now = datetime.now(UTC)
        slug, title, _summary, content_md, citations = synthesize_blog_content(topic, [], now)

        assert "ai_coding" in slug
        assert "AI Coding" in title
        assert "Autonomous Coding Agent" in content_md
        assert len(content_md) > 200
        assert isinstance(citations, list)

    async def test_api_digests_flow(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_digests.db"

        import app.api.deps as deps
        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
        await dispose_engine()
        deps.reset_pipelines()
        await ensure_schema()

        client = TestClient(create_app())

        # 1. Get topics
        resp = client.get("/v1/digests/topics")
        assert resp.status_code == 200
        topics = resp.json()
        assert len(topics) >= 5
        topic_ids = [t["id"] for t in topics]
        assert "ai_coding" in topic_ids
        assert "viet_finance" in topic_ids

        # 2. Trigger scan on-demand for 'ai_coding'
        scan_resp = client.post("/v1/digests/scan", json={"topic": "ai_coding"})
        assert scan_resp.status_code == 200
        created = scan_resp.json()
        assert len(created) == 1
        post_slug = created[0]["slug"]
        post_id = created[0]["id"]

        # 3. List digests
        list_resp = client.get("/v1/digests")
        assert list_resp.status_code == 200
        items = list_resp.json()
        assert len(items) >= 1
        assert any(i["slug"] == post_slug for i in items)

        # 4. Get digest detail by slug
        detail_resp = client.get(f"/v1/digests/{post_slug}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["slug"] == post_slug
        assert "content_md" in detail
        assert len(detail["content_md"]) > 100

        # 5. Get digest detail by ID
        id_resp = client.get(f"/v1/digests/{post_id}")
        assert id_resp.status_code == 200
        assert id_resp.json()["id"] == post_id


class TestNotificationDispatcher:
    async def test_empty_config_skips_silently(self):
        settings = Settings(
            telegram_bot_token="",
            telegram_chat_id="",
            webhook_url="",
            discord_webhook_url="",
        )
        dispatcher = NotificationDispatcher(settings)
        post = BlogPost(title="Test", summary="Summary", slug="test", topic="ai_coding")
        res = await dispatcher.send_briefing_notification(post)
        assert res == {"telegram": False, "webhook": False, "discord": False}

    async def test_dispatches_to_all_channels_successfully(self, patched_httpx):
        settings = Settings(
            telegram_bot_token="bot_12345",
            telegram_chat_id="chat_67890",
            webhook_url="https://api.example.com/webhook",
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            calls.append(url_str)
            if "api.telegram.org" in url_str:
                return httpx.Response(200, json={"ok": True})
            elif "discord.com" in url_str:
                return httpx.Response(204)
            elif "api.example.com" in url_str:
                return httpx.Response(200, json={"status": "received"})
            return httpx.Response(404)

        patched_httpx("app.orchestration.notifier", handler)

        dispatcher = NotificationDispatcher(settings)
        post = BlogPost(
            title="Đột phá AI Coding",
            summary="Cursor và Sonnet tối ưu hóa quy trình",
            slug="ai-coding-2026",
            topic="ai_coding",
            tags=["ai-coding"],
            sources=[{"title": "MetaSearch", "url": "https://metasearch.ai"}],
        )

        res = await dispatcher.send_briefing_notification(post)
        assert res.get("telegram") is True
        assert res.get("webhook") is True
        assert res.get("discord") is True
        assert len(calls) == 3

    async def test_dispatcher_handles_network_failure_gracefully(self, patched_httpx):
        settings = Settings(
            telegram_bot_token="bot_fail",
            telegram_chat_id="chat_fail",
            webhook_url="https://api.example.com/failing-webhook",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        patched_httpx("app.orchestration.notifier", handler)

        dispatcher = NotificationDispatcher(settings)
        post = BlogPost(title="Test", summary="Summary", slug="test", topic="ai_coding")
        # Must not raise an exception
        res = await dispatcher.send_briefing_notification(post)
        assert res.get("telegram") is False
        assert res.get("webhook") is False


class TestDeltaHighlighting:
    def test_build_delta_section_without_previous_post(self):
        section = _build_delta_section(None, [])
        assert "Bối Cảnh Khởi Điểm" in section

    def test_build_delta_section_with_fresh_items(self):
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        prev_post = BlogPost(
            title="Kỳ trước",
            slug="ky-truoc",
            topic="ai_coding",
            sources=[{"title": "Cũ 1", "url": "https://example.com/old1"}],
            published_at=now,
        )

        current_results = [
            RawResult(
                url="https://example.com/fresh-agent",
                title="Ra mắt mô hình Coding Agent mới",
                snippet="Hiệu năng tăng 40% trên HumanEval",
                adapter="test",
                rank=1,
            )
        ]

        section = _build_delta_section(prev_post, current_results)
        assert "Điểm Mới Trong 24 Giờ Qua" in section
        assert "Phát Hiện Mới" in section
        assert "Ra mắt mô hình Coding Agent mới" in section

    def test_build_delta_section_with_no_delta(self):
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        prev_post = BlogPost(
            title="Kỳ trước",
            slug="ky-truoc",
            topic="ai_coding",
            sources=[{"title": "Tin đã biết", "url": "https://example.com/known"}],
            published_at=now,
        )

        current_results = [
            RawResult(
                url="https://example.com/known",
                title="Tin đã biết",
                snippet="Nội dung giữ nguyên",
                adapter="test",
                rank=1,
            )
        ]

        section = _build_delta_section(prev_post, current_results)
        assert "Nhịp Vận Động" in section
        assert "tiếp tục nhất quán" in section
