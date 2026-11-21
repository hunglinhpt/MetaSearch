"""docs/33 — Expert Briefing Engine + registry + endpoints.

Covers: registry shape + JSON operator overrides, deterministic evidence
pipeline (strict window, thread clustering, authority ranking), Step A
grounding verifier, Step B council review + revision, extractive fallback,
and endpoint gating (admin + per-channel single-flight).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.gateway import Usage
from app.orchestration.briefing_engine import (
    BriefingEngine,
    _strip_unverified,
    collect_channel_evidence,
)
from app.orchestration.briefing_registry import (
    BRIEFING_CHANNELS,
    DEEP_CHANNEL_ORDER,
    NEWS_CHANNEL_ORDER,
    load_channel_overrides,
)

_NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def _result(url, title, snippet, adapter="google_news", published_at=None, source_name=""):
    return SimpleNamespace(
        url=url,
        title=title,
        snippet=snippet,
        adapter=adapter,
        published_at=published_at,
        source_name=source_name,
    )


class _Cfg:
    def __init__(self, engine_id: str, category: str):
        self.id = engine_id
        self.category = category
        self.timeout_ms = 3000


class _Registry:
    """Items are (engine_id, category, adapter) tuples."""

    def __init__(self, adapters):
        self._adapters = adapters

    async def enabled(self):
        return [(self._cfg(e[0], e[1]), e[2]) for e in self._adapters]

    @staticmethod
    def _cfg(engine_id: str, category: str):
        return _Cfg(engine_id, category)


def _channel(**overrides):
    from app.orchestration.briefing_registry import BriefingChannel

    base = {
        "id": "macro_economy",
        "name": "Tin kinh tế vĩ mô",
        "kind": "news",
        "window_hours": 72,
        "persona_name": "Chief Macroeconomist",
        "persona_rules": "Phân tích vĩ mô.",
        "engine_categories": ["news", "financial"],
        "description": "d",
        "tags": ["vi-mo"],
    }
    base.update(overrides)
    return BriefingChannel(**base)


# --- registry --------------------------------------------------------------------


def test_registry_shape_personas_and_windows():
    assert len(NEWS_CHANNEL_ORDER) == 6
    assert set(NEWS_CHANNEL_ORDER) <= set(BRIEFING_CHANNELS)
    assert set(DEEP_CHANNEL_ORDER) <= set(BRIEFING_CHANNELS)
    for ch in BRIEFING_CHANNELS.values():
        assert ch.persona_name, f"{ch.id} missing persona"
        assert ch.persona_rules, f"{ch.id} missing persona rules"
        assert ch.window_hours >= 24
        assert ch.engine_categories
    # 48h channel maps to 2 adapter days; 72h -> 3
    assert BRIEFING_CHANNELS["markets_stocks"].freshness_days == 2
    assert BRIEFING_CHANNELS["macro_economy"].freshness_days == 3


def test_json_override_adds_channel_without_code(tmp_path: Path):
    override = tmp_path / "channels.json"
    override.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "id": "energy_transition",
                        "name": "Chuyển dịch năng lượng trong 3 ngày",
                        "kind": "news",
                        "window_hours": 72,
                        "persona_name": "Energy Markets Analyst",
                        "persona_rules": "Phân tích năng lượng.",
                        "engine_categories": ["news"],
                        "description": "d",
                        "tags": ["energy"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    channels = load_channel_overrides(override)
    assert "energy_transition" in channels
    assert channels["energy_transition"].persona_name == "Energy Markets Analyst"
    # built-ins untouched
    assert "macro_economy" in channels
    # invalid rows skipped, never break the registry
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"channels": [{"name": "no id"}, "junk"]}), encoding="utf-8")
    assert "macro_economy" in load_channel_overrides(bad)


# --- evidence pipeline -------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_strict_window_filters_undated_and_stale():
    in_window = _result(
        "https://a.example/1",
        "Tin trong cửa sổ",
        "nội dung",
        published_at=_NOW - timedelta(hours=10),
    )
    stale = _result(
        "https://a.example/2",
        "Tin 2024 cũ",
        "nội dung cũ",
        published_at=_NOW - timedelta(days=400),
    )
    undated = _result("https://a.example/3", "Tin không ngày", "nội dung")
    registry = _Registry([("google_news", "news", _CaptureAdapter([in_window, stale, undated]))])
    channel = _channel(window_hours=72)
    evidence, _raw, dropped = await collect_channel_evidence(
        channel, registry, now=_NOW, max_evidence=10
    )
    assert dropped == 1
    assert [e.url for e in evidence] == ["https://a.example/1"]


class _CaptureAdapter:
    def __init__(self, results):
        self.results = results
        self.calls: list[dict] = []

    async def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.results


@pytest.mark.asyncio
async def test_evidence_fanout_passes_freshness_and_clusters_dupes():
    adapter = _CaptureAdapter(
        [
            _result(
                "https://x.example/1",
                "Fed giữ lãi suất",
                "FOMC giữ nguyên mức lãi suất",
                published_at=_NOW - timedelta(hours=5),
            ),
            _result(
                "https://y.example/syndicated",
                "Fed giữ lãi suất: FOMC giữ nguyên mức lãi suất",
                "FOMC giữ nguyên mức lãi suất",
                adapter="bing_news",
                published_at=_NOW - timedelta(hours=6),
            ),
        ]
    )
    registry = _Registry([("google_news", "news", adapter)])
    channel = _channel(window_hours=72)
    evidence, _raw, _dropped = await collect_channel_evidence(
        channel, registry, now=_NOW, max_evidence=10
    )
    assert adapter.calls and adapter.calls[0]["freshness_days"] == 3
    # syndicated near-dup folds into ONE event thread with both engines
    assert len(evidence) == 1
    assert set(evidence[0].engines) == {"google_news", "bing_news"}
    assert len(evidence[0].thread_members) == 2


# --- grounding verifier -------------------------------------------------------------


def _ev(n: int, title: str, snippet: str, url: str = "https://e.example/x", published=None):
    from app.orchestration.briefing_engine import EvidenceItem

    return EvidenceItem(
        n=n,
        title=title,
        snippet=snippet,
        url=url,
        source_name="Example",
        published=published,
        engines=["google_news"],
        tier_score=1.0,
        thread_members=[url],
    )


def test_strip_unverified_removes_out_of_range_refs():
    evidence = [_ev(1, "T1", "S1"), _ev(2, "T2", "S2")]
    draft = "Câu một [1] ok. Câu hai trỏ sai [9]."
    cleaned, sentences, refs = _strip_unverified(draft, evidence)
    assert "[9]" not in cleaned
    assert "[1]" in cleaned
    assert sentences == 0
    assert refs == 1


def test_strip_unverified_drops_unmatched_numbers():
    evidence = [_ev(1, "Lạm phát 3,2%", "CPI tăng 3,2%")]
    draft = "Lạm phát đạt 3,2% [1]. Lãi suất có thể lên 9,7% trong quý tới. Không có số ở câu này."
    cleaned, sentences, _refs = _strip_unverified(draft, evidence)
    assert "3,2%" in cleaned
    assert "9,7%" not in cleaned  # fabricated number stripped
    assert sentences == 1


def test_strip_unverified_keeps_dates_from_packet():
    """docs/33 audit P1-2 regression: the verifier matches against the FULL
    packet (dates + source names + URLs included) plus today's date — a
    legitimately dated sentence the packet fed the model must survive."""
    pub = _NOW - timedelta(days=1)
    evidence = [_ev(1, "Fed giữ lãi suất", "FOMC giữ nguyên mức", published=pub)]
    packet_date = pub.strftime("%d/%m/%Y")
    draft = (
        f"# Bản Tin Ngày {_NOW.strftime('%d/%m/%Y')}\n\n"
        f"Ngày {packet_date}, Fed giữ lãi suất [1].\n"
        "## Góc nhìn chuyên gia & Điểm cần theo dõi\nTheo dõi CPI."
    )
    cleaned, sentences, _refs = _strip_unverified(draft, evidence, today=_NOW)
    assert sentences == 0
    assert "Ngày" + " " + packet_date in cleaned or f"Ngày {packet_date}" in cleaned
    assert "Fed giữ lãi suất" in cleaned


# --- engine + fallback + review ------------------------------------------------------


class _StubGateway:
    provider = "openai"

    def __init__(self, draft: str, reviews: list[str] | None = None, fail=False):
        self.draft = draft
        self.reviews = reviews or []
        self.fail = fail
        self.calls: list[str] = []

    async def complete(self, messages, *, model=None, max_tokens=None, task="intent"):
        self.calls.append(task)
        if self.fail:
            raise RuntimeError("provider down")
        if task == "briefing_review":
            text = self.reviews.pop(0) if self.reviews else "{}"
            return text, Usage(input_tokens=10, output_tokens=5)
        return self.draft, Usage(input_tokens=10, output_tokens=100)


@pytest.mark.asyncio
async def test_generate_ai_grounded_with_council_pass():
    adapter = _CaptureAdapter(
        [
            _result(
                "https://m.example/1",
                "Lạm phát giảm về 2,9%",
                "CPI tháng này giảm về mức 2,9% theo tổng cục thống kê",
                published_at=_NOW - timedelta(hours=8),
            )
        ]
    )
    engine = BriefingEngine(
        _Registry([("google_news", "news", adapter)]),
        _StubGateway(
            "## Bức tranh tổng quan\nLạm phát giảm về 2,9% [1].\n"
            "## Các diễn biến trọng yếu\nNhóm CPI: What -> Why -> tác động [1].\n"
            "## Góc nhìn chuyên gia & Điểm cần theo dõi\nTheo dõi CPI tháng sau.",
            reviews=[
                '{"hallucination_free": true, "depth_ok": true, "issues": []}',
                '{"hallucination_free": true, "depth_ok": true, "issues": []}',
            ],
        ),
    )
    channel = _channel()
    result = await engine.generate(channel, now=_NOW)
    assert result.ai_grounded is True
    assert result.evidence_count == 1
    assert len(result.review_verdicts) == 2
    assert "Bức tranh tổng quan" in result.content_md
    assert "Nguồn Dữ Liệu" in result.content_md
    # review calls happened after synthesis
    assert result.stripped_unverified == 0


@pytest.mark.asyncio
async def test_generate_triggers_revision_on_review_fail():
    adapter = _CaptureAdapter(
        [
            _result(
                "https://m.example/1",
                "Lạm phát giảm về 2,9%",
                "CPI tháng này giảm về mức 2,9%",
                published_at=_NOW - timedelta(hours=8),
            )
        ]
    )
    gateway = _StubGateway(
        "## Bức tranh tổng quan\nNháp chưa sâu.\n"
        "## Các diễn biến trọng yếu\nSơ sài.\n"
        "## Góc nhìn chuyên gia & Điểm cần theo dõi\nSơ sài.",
        reviews=[
            '{"hallucination_free": false, "depth_ok": false, '
            '"issues": ["thiếu phân tích nhân quả"]}',
            '{"hallucination_free": true, "depth_ok": true, "issues": []}',
        ],
    )
    gateway.complete_original = gateway.complete

    async def complete(self, messages, **kw):
        self.calls.append(str(kw.get("task")))
        if kw.get("task") == "briefing_revision":
            return (
                "## Bức tranh tổng quan\nLạm phát giảm về 2,9% [1].\n"
                "## Các diễn biến trọng yếu\nWhat -> Why -> tác động [1].\n"
                "## Góc nhìn chuyên gia & Điểm cần theo dõi\nTheo dõi CPI.",
                Usage(input_tokens=10, output_tokens=50),
            )
        return await self.complete_original(messages, **kw)

    gateway.complete = complete.__get__(gateway)  # type: ignore[method-assign]
    engine = BriefingEngine(_Registry([("google_news", "news", adapter)]), gateway)
    result = await engine.generate(_channel(), now=_NOW)
    assert "briefing_revision" in gateway.calls
    assert result.ai_grounded is True


@pytest.mark.asyncio
async def test_generate_falls_back_to_extractive_when_ai_down():
    adapter = _CaptureAdapter(
        [
            _result(
                "https://m.example/1",
                "Tin thật trong cửa sổ",
                "nội dung thật",
                published_at=_NOW - timedelta(hours=3),
            )
        ]
    )
    engine = BriefingEngine(
        _Registry([("google_news", "news", adapter)]), _StubGateway("", fail=True)
    )
    result = await engine.generate(_channel(), now=_NOW)
    assert result.ai_grounded is False
    assert "extractive" in result.summary.lower() or "extractive" in result.content_md.lower()
    assert "Tin thật trong cửa sổ" in result.content_md


@pytest.mark.asyncio
async def test_generate_empty_evidence_honest():
    adapter = _CaptureAdapter([])
    engine = BriefingEngine(_Registry([("google_news", "news", adapter)]), _StubGateway("x"))
    result = await engine.generate(_channel(), now=_NOW)
    assert result.evidence_count == 0
    assert "không suy diễn" in result.content_md.lower()


# --- endpoint gating ------------------------------------------------------------------


def test_briefing_channels_endpoint(client):
    resp = client.get("/v1/briefings/channels")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["news_channels"]) == 6
    assert len(body["deep_channels"]) == 5
    assert all(c["persona_name"] for c in body["news_channels"] + body["deep_channels"])


def test_briefing_unknown_channel_400(client):
    resp = client.post("/v1/briefings/no_such_channel")
    assert resp.status_code == 400
    assert "Valid channels" in resp.json()["error"]["message"]
