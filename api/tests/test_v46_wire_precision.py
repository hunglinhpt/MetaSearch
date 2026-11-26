"""docs/35 M1-M3: news-wire symbol precision, category reasons, publication dates.

Gold sets (tests/fixtures/*_gold.jsonl): real headlines from the live wire
(2026-11-23..26, hand-labelled; ``origin`` marks every row), the consuming
system's evidence rows, and a few clearly-marked constructed rows for thin
classes. Pure tests — no network.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.retrieval.aliases import is_generic_alias, reset_alias_index, resolve_alias_matches
from app.retrieval.events import (
    ParsedNewsItem,
    _enrich_missing_dates,
    _hydrate_evidence,
    decide_news_category,
    extract_ticker_evidence,
    extract_tickers_from_text,
    parse_rss_feed_xml,
)
from app.retrieval.health import EngineBreakerRegistry
from app.retrieval.news_dates import parse_published, published_from_html, published_from_url
from app.retrieval.symbols.evidence import SymbolEvidence, split_by_field
from app.retrieval.symbols.geo_guard import mask_geo
from app.retrieval.symbols.index_codes import INDEX_CODES, extract_indices

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    with open(FIX / name, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------------------
# M1 — gold acceptance: precision >= 0.95, recall >= 0.85, 0 HCM for "TP.HCM"-only
# headlines, 0 index codes in symbols.
# --------------------------------------------------------------------------------------


def test_symbol_gold_set_meets_acceptance() -> None:
    reset_alias_index()
    rows = _load("vn_headlines_symbols_gold.jsonl")
    assert len(rows) >= 60
    # Headlines naming the city (any spelling) with no HCM ticker in the gold labels.
    city_only = [
        r for r in rows if mask_geo(r["title"]) != r["title"] and "HCM" not in r["symbols"]
    ]
    assert len(city_only) >= 10
    tp = fp = fn = 0
    for r in rows:
        got = set(extract_tickers_from_text(r["title"]))
        want = set(r["symbols"])
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        assert not (got & INDEX_CODES - want), r["title"]
        if "HCM" not in want:
            assert "HCM" not in got, r["title"]
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    assert precision >= 0.95, (precision, recall)
    assert recall >= 0.85, (precision, recall)


@pytest.mark.parametrize(
    "title,want",
    [
        ("TP.HCM thu hồi gần 4 ha đất của liên doanh Sabeco", ["SAB"]),
        ("Intel muốn nâng vị thế TP.HCM trong chuỗi bán dẫn", []),
        ("Lãi suất cao ghìm sức mua, doanh nghiệp bất động sản", []),
        ("Tập đoàn Công nghệ CMC dự kiến trả cổ tức bằng cổ phiếu tỷ lệ 10%", ["CMG"]),
        ("CMG muốn phát hành hơn 23 triệu cổ phiếu trả cổ tức tỷ lệ 10%", ["CMG"]),
        ("Kỷ niệm 30 năm Ngày truyền thống ngành Chứng khoán", []),
        ("VinFast bàn giao 10.000 xe điện", []),
        ("Cen Land thoái vốn công ty con thứ 4", ["CRE"]),
    ],
)
def test_proposal_rows(title: str, want: list[str]) -> None:
    assert extract_tickers_from_text(title) == want


def test_evidence_methods_offsets_and_strongest_first() -> None:
    ev = {e.symbol: e for e in extract_ticker_evidence("HCM: (HOSE: FPT) và cổ phiếu VNM, $NVDA")}
    assert ev["HCM"].method == "prefix" and ev["HCM"].start == 0
    assert ev["FPT"].method == "exchange"
    assert ev["VNM"].method == "phrase"
    text = "Cổ phiếu HCM tăng; Chứng khoán HSC báo lãi"
    hcm = extract_ticker_evidence(text)
    assert [(e.symbol, e.method) for e in hcm] == [("HCM", "phrase")]  # one record per symbol
    assert text[hcm[0].start : hcm[0].start + 3] == "HCM"


def test_alias_evidence_has_confidence_and_span() -> None:
    (e,) = extract_ticker_evidence("Tập đoàn Công nghệ CMC dự kiến trả cổ tức")
    assert e.method == "alias" and e.symbol == "CMG"
    assert e.span == "Tập đoàn Công nghệ CMC" and e.alias_confidence and e.alias_confidence >= 0.9


def test_index_codes_need_explicit_evidence() -> None:
    assert extract_tickers_from_text("VN-Index giảm, VNI mất mốc 1.700") == []
    assert extract_tickers_from_text("UPCOM: VNI họp ĐHĐCĐ bất thường") == ["VNI"]
    assert extract_indices("VN-Index và VN30 cùng giảm, HNX-Index tăng") == [
        "VNINDEX",
        "VN30",
        "HNXINDEX",
    ]


def test_mask_geo_preserves_offsets() -> None:
    src = "Kinh tế TP.HCM, Tp HCM và TPHCM; Thành phố Hồ Chí Minh"
    masked = mask_geo(src)
    assert len(masked) == len(src)
    assert "HCM" not in masked and "Hồ Chí Minh" not in masked
    assert masked.startswith("Kinh tế ")


def test_split_by_field() -> None:
    ev = [SymbolEvidence("AAA", "bare", "AAA", 0), SymbolEvidence("BBB", "bare", "BBB", 10)]
    out = split_by_field(ev, title_len=5)
    assert [(e.field, e.start) for e in out] == [("title", 0), ("snippet", 4)]


class TestAliasPrecisionGates:
    def test_generic_and_place_aliases_are_not_indexed(self) -> None:
        for phrase in ("Bất động sản", "Hải Phòng", "Truyền thông", "Cấp nước Hải Phòng"):
            assert is_generic_alias(phrase), phrase
        assert not is_generic_alias("Nhựa Bình Minh")
        assert not is_generic_alias("Dầu khí IDICO")

    def test_accent_exact(self) -> None:
        assert resolve_alias_matches("Ngày truyền thống ngành") == []
        assert [m.symbol for m in resolve_alias_matches("Bia Sai Gon tang truong")] == ["SAB"]

    def test_tone_mark_variants_are_equal(self) -> None:
        assert [m.symbol for m in resolve_alias_matches("Người nội bộ Đá Hoá An bán")] == ["DHA"]

    def test_proper_noun_casing_for_auto_aliases(self) -> None:
        hits = resolve_alias_matches("Nhựa Bình Minh tăng giá")
        assert [h.symbol for h in hits] == ["BMP"]
        assert resolve_alias_matches("nhựa bình minh tăng giá") == []

    def test_short_acronyms_are_case_sensitive(self) -> None:
        assert [m.symbol for m in resolve_alias_matches("HSC báo lãi")] == ["HCM"]
        assert resolve_alias_matches("hsc báo lãi") == []


def test_cached_evidence_hydration_is_defensive() -> None:
    raw = [
        {"symbol": "FPT", "method": "exchange", "span": "FPT", "start": 7, "field": "title"},
        {"symbol": "X", "method": "made_up"},
        "garbage",
        {"symbol": "SAB", "method": "alias", "start": True, "alias_confidence": 0.97},
    ]
    out = _hydrate_evidence(raw)
    assert [(e.symbol, e.method, e.start) for e in out] == [
        ("FPT", "exchange", 7),
        ("SAB", "alias", 0),
    ]
    assert out[1].alias_confidence == 0.97
    assert _hydrate_evidence(None) == []


# --------------------------------------------------------------------------------------
# M2 — category gold: macro-F1 >= 0.85, regulatory precision >= 0.95
# --------------------------------------------------------------------------------------


def test_category_gold_set_meets_acceptance() -> None:
    rows = _load("vn_headlines_category_gold.jsonl")
    counts = Counter(r["category"] for r in rows)
    assert len(rows) >= 80 and min(counts.values()) >= 8, counts
    conf: Counter[tuple[str, str]] = Counter()
    for r in rows:
        syms = extract_tickers_from_text(f"{r['title']} {r['snippet'][:200]}")
        d = decide_news_category(r["title"], r["snippet"], symbols=syms)
        assert d.reason and 0 < d.confidence <= 1
        conf[(r["category"], d.category)] += 1
    f1s = []
    precisions = {}
    for c in counts:
        tp = conf[(c, c)]
        fp = sum(v for (g, p), v in conf.items() if p == c and g != c)
        fn = sum(v for (g, p), v in conf.items() if g == c and p != c)
        pr = tp / max(1, tp + fp)
        rc = tp / max(1, tp + fn)
        precisions[c] = pr
        f1s.append(2 * pr * rc / max(1e-9, pr + rc))
    assert sum(f1s) / len(f1s) >= 0.85, f1s
    assert precisions["regulatory"] >= 0.95


@pytest.mark.parametrize(
    "title,category,scope",
    [
        ("Giá vàng lao dốc, chuyên gia đưa ra dự báo bất ngờ", "general_finance", None),
        ("Cảnh báo sớm rủi ro với nhóm hàng xuất siêu lớn", "general_finance", None),
        (
            "Chứng khoán Mỹ bán tháo dù cổ phiếu Anthropic tăng 17% trong phiên chào sàn",
            "general_finance",
            "global",
        ),
        ("Quốc hội chốt trần phạt hành chính 3 tỷ đồng", "macro_policy", "policy"),
        ("TDW bị xử phạt và truy thu thuế hơn 1.4 tỷ", "regulatory", "ticker"),
        ("HPG bị đưa vào diện cảnh báo trên HOSE", "regulatory", "ticker"),
        ("Cen Land thoái vốn công ty con thứ 4", "corporate_action", "ticker"),
        ("Cổ phiếu DRH bị hủy niêm yết bắt buộc trên HOSE", "listing_status", None),
    ],
)
def test_category_rules(title: str, category: str, scope: str | None) -> None:
    d = decide_news_category(title, "", symbols=extract_tickers_from_text(title))
    assert d.category == category, d
    if scope:
        assert d.scope == scope, d


# --------------------------------------------------------------------------------------
# M3 — publication dates
# --------------------------------------------------------------------------------------

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def test_date_from_article_meta() -> None:
    html = (
        '<html><head><meta property="article:published_time" '
        'content="2026-11-25T08:30:00+07:00"></head></html>'
    )
    dt, src = published_from_html(html, now=NOW)
    assert src == "article_meta" and dt == datetime(2026, 11, 25, 1, 30, tzinfo=UTC)


def test_date_from_json_ld_graph_and_naive_is_vietnam_time() -> None:
    html = (
        '<script type="application/ld+json">{"@graph": [{"@type": "WebPage"},'
        ' {"@type": "NewsArticle", "datePublished": "2026-11-24 09:15:00"}]}</script>'
    )
    dt, src = published_from_html(html, now=NOW)
    assert src == "json_ld" and dt == datetime(2026, 11, 24, 2, 15, tzinfo=UTC)


def test_date_from_url_and_implausible_dates() -> None:
    assert published_from_url("https://x.vn/2026/11/23/tin.html", now=NOW) == datetime(
        2026, 11, 22, 17, 0, tzinfo=UTC
    )
    assert published_from_url("https://x.vn/bai-viet-20261120123456.htm", now=NOW) is not None
    assert published_from_url("https://x.vn/2099/01/01/future.html", now=NOW) is None
    assert published_from_url("https://x.vn/no-date-here.htm", now=NOW) is None
    assert published_from_html("<html>no dates</html>", now=NOW) == (None, None)
    assert parse_published("25/11/2026 14:05") == datetime(2026, 11, 25, 7, 5, tzinfo=UTC)
    assert parse_published("garbage") is None


async def test_enrichment_fills_dates_and_never_guesses(monkeypatch) -> None:
    from app.retrieval import events as ev_mod

    pages = {
        "https://a.vn/x.htm": (
            '<meta property="article:published_time" content="2026-11-25T08:00:00+07:00">'
        ),
        "https://b.vn/y.htm": "<html>no date anywhere</html>",
    }
    calls: list[str] = []

    async def fake_fetch(source_id, url, **kwargs):
        calls.append(url)
        return pages.get(url), None

    monkeypatch.setattr(ev_mod, "_fetch_source_text", fake_fetch)
    ev_mod._DATE_MEMO.clear()
    items = [
        ParsedNewsItem(title="A", url="https://a.vn/x.htm", source_id="s"),
        ParsedNewsItem(title="B", url="https://b.vn/y.htm", source_id="s"),
        ParsedNewsItem(title="C", url="https://c.vn/2026/11/24/z.htm", source_id="s"),
        ParsedNewsItem(title="D", url="https://d.vn/q", source_id="s", event_date="2026-12-01"),
    ]
    import time

    gained = await _enrich_missing_dates(
        items,
        deadline=time.monotonic() + 5,
        breakers=EngineBreakerRegistry(threshold=3, timeout_threshold=5),
        client=None,
    )
    assert gained == 2
    assert items[0].published_at_source == "article_meta"
    assert items[1].published_at is None and items[1].published_at_source is None
    assert items[2].published_at_source == "url"
    assert items[3].published_at is None  # event-dated items are left alone
    # Memoised: a second run fetches nothing.
    calls.clear()
    for it in items[:2]:
        it.published_at = None
    await _enrich_missing_dates(
        items[:2],
        deadline=time.monotonic() + 5,
        breakers=EngineBreakerRegistry(threshold=3, timeout_threshold=5),
        client=None,
    )
    assert calls == [] and items[0].published_at is not None


def test_rss_items_carry_offset_and_source() -> None:
    xml = """<?xml version="1.0"?><rss><channel><item><title>TDW bị xử phạt và truy thu thuế</title>
    <link>https://vietstock.vn/a.htm</link><pubDate>Wed, 25 Nov 2026 22:57:13 +0700</pubDate>
    </item></channel></rss>"""
    (item,) = parse_rss_feed_xml(xml, source_id="vs")
    assert item.published_at is not None and item.published_at.utcoffset() is not None
    assert item.published_at_source == "feed"
    assert item.category == "regulatory" and item.category_confidence >= 0.9
    assert [e.method for e in item.symbol_evidence] == ["bare"]
