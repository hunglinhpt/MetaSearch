"""docs/36 §20: answer context quality — off-topic gate (Q1), rank-aware scrape exit (Q2)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace as NS

from app.config import Settings
from app.extraction.scraper import PageContent, PageFailure, Scraper
from app.retrieval.relevance_gate import drop_off_topic, off_topic_flags, query_anchors

PG_Q = "what is PostgreSQL logical replication and how does it differ from physical replication"
# Real mwmbl/arxiv batch from the 2026-11-27 diagnosis (titles verbatim).
PG_BATCH = [
    (
        "http://pgdash.io/blog/postgres-physical-replication.html",
        "Physical Replication Mechanisms in PostgreSQL - pgDash",
    ),
    ("http://arxiv.org/abs/1508.05233v4", "Super-replication in Fully Incomplete Markets"),
    (
        "https://www.comparitech.com/vpn/what-is-a-vnc",
        "What is a VNC and how does it differ from a VPN?",
    ),
    (
        "https://stackoverflow.com/q/48155294",
        "python - What is pycryptodomex and how does it differ from pycryptodome?",
    ),
    (
        "https://www.pcgamer.com/what-is-ray-tracing/",
        "What is ray tracing, and how does it differ from game to game?",
    ),
    (
        "https://www.androidauthority.com/what-is-claude-ai-chatbot-3395332/",
        "What is Claude AI and how does it differ from ChatGPT?",
    ),
]


def _items(batch):
    return [NS(url=u, title=t, snippet="") for u, t in batch]


def test_anchors_are_entities_not_sentence_starters():
    assert query_anchors(PG_Q) == {"postgresql"}
    assert query_anchors("Redis pub sub khác gì Stream như thế nào") == {"redis", "stream"}
    assert "sec" in query_anchors("SEC 10-K filing requirements for foreign private issuers")
    assert query_anchors("Cách tính thuế TNCN") == {"tncn"}
    assert query_anchors("cach nau pho bo") == set()


def test_boilerplate_matches_are_off_topic():
    flags = off_topic_flags(PG_Q, _items(PG_BATCH))
    assert flags == [False, True, True, True, True, True]
    kept, dropped = drop_off_topic(PG_Q, _items(PG_BATCH))
    assert [k.title for k in kept] == [PG_BATCH[0][1]] and dropped == 5


def test_anchor_protects_weak_but_relevant_results():
    q = "Redis pub sub khác gì Stream như thế nào"
    batch = [
        ("https://redis.io/blog/pubsub", "SDN-like: The Next Generation of Pub/Sub Stream"),
        ("https://redis.io/docs/", "Docs - Redis"),
        ("https://xoso.example/kq", "Kết quả xổ số miền Nam hôm nay"),
    ]
    assert off_topic_flags(q, _items(batch)) == [False, False, True]


def test_gate_never_empties_and_skips_short_queries():
    weak = [("https://a.example", "alpha"), ("https://b.example", "beta gamma")]
    assert not all(off_topic_flags("alpha beta delta epsilon", _items(weak)))
    assert off_topic_flags("postgresql", _items(PG_BATCH)) == [False] * len(PG_BATCH)
    assert off_topic_flags(PG_Q, _items(PG_BATCH[:1])) == [False]


def test_on_topic_copies_the_shared_outcome():
    from app.orchestration.pipeline import SearchOutcome, _on_topic

    results = _items(PG_BATCH)
    outcome = SearchOutcome(query_id=uuid.uuid4(), results=results, statuses=[], latency_ms=1)
    narrowed = _on_topic(outcome, PG_Q)
    assert len(narrowed.results) == 1 and len(outcome.results) == len(PG_BATCH)
    clean = SearchOutcome(query_id=uuid.uuid4(), results=results[:1], statuses=[], latency_ms=1)
    assert _on_topic(clean, PG_Q) is clean


async def test_scrape_early_exit_waits_for_top_ranked_pages(monkeypatch):
    settings = Settings(
        _env_file=None,
        scrape_max_urls=8,
        scrape_min_pages=3,
        scrape_soft_budget_s=0.1,
        scrape_timeout_s=3.0,
        scrape_url_budget_s=3.0,
        scrape_concurrency=8,
    )
    scraper = Scraper(settings)

    async def fake(url: str, timeout_s: float | None = None):
        await asyncio.sleep(0.4 if "top" in url else (0.01 if "fast" in url else 5.0))
        return PageContent(
            url=url,
            final_url=url,
            title=url,
            markdown=f"content of {url} long enough",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(scraper, "_scrape_guarded", fake)
    urls = [
        "https://top1.example",
        "https://fast2.example",
        "https://fast3.example",
        "https://fast4.example",
        "https://slow5.example",
    ]
    start = time.monotonic()
    results = await scraper.scrape_many(urls)
    elapsed = time.monotonic() - start
    by_url = {r.url: r for r in results}
    assert isinstance(by_url["https://top1.example"], PageContent)  # not cancelled
    assert isinstance(by_url["https://slow5.example"], PageFailure)  # tail still cut
    assert 0.35 <= elapsed < 1.5


# --- Q4: answer language reaches the prompt ---------------------------------------------


def test_answer_language_detection():
    from app.llm.prompts import answer_language

    assert answer_language("FastAPI dependency injection override for testing", "en") == "English"
    assert answer_language("anything", "vi-VN") == "Vietnamese"
    assert answer_language("cách nấu phở bò ngon", "auto") == "Vietnamese"
    assert answer_language("what is reciprocal rank fusion", None) == "English"
    # Unaccented Vietnamese is never mistaken for English ("the" = "thể", "to" = "tô").
    assert answer_language("cach tinh thue thu nhap ca nhan co the", "auto") is None
    assert answer_language("bitcoin", None) is None


def test_language_directive_follows_the_question_and_keeps_system_prefix():
    from app.llm.prompts import build_messages

    plain = build_messages("BERT vs RoBERTa", [])
    en = build_messages("BERT vs RoBERTa", [], language="English")
    assert plain[0] == en[0]  # system prefix byte-identical (prompt cache)
    assert en[1]["content"].endswith(
        "Answer language: English. Write the whole answer in English, "
        "even where the sources use another language."
    )
    assert "Answer language" not in plain[1]["content"]


async def test_top_rank_wait_is_capped_by_grace(monkeypatch):
    """The LLM keeps its share of the answer budget: a hung top page is given up."""
    settings = Settings(
        _env_file=None,
        scrape_min_pages=3,
        scrape_soft_budget_s=0.1,
        scrape_top_grace_s=0.2,
        scrape_timeout_s=3.0,
        scrape_url_budget_s=3.0,
        scrape_concurrency=8,
    )
    scraper = Scraper(settings)

    async def fake(url: str, timeout_s: float | None = None):
        await asyncio.sleep(3.0 if "top" in url else 0.01)
        return PageContent(
            url=url,
            final_url=url,
            title=url,
            markdown="content long enough",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(scraper, "_scrape_guarded", fake)
    urls = ["https://top1.example"] + [f"https://fast{i}.example" for i in range(2, 6)]
    start = time.monotonic()
    results = await scraper.scrape_many(urls)
    assert time.monotonic() - start < 0.8
    assert isinstance({r.url: r for r in results}["https://top1.example"], PageFailure)
