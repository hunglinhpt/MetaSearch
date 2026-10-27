"""Unit tests for URL canonicalization, scoring and the merger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.retrieval.canonical import canonicalize
from app.retrieval.merger import diversity_cap, merge
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.scoring import (
    consensus_score,
    freshness,
    heuristic_chunk_score,
    keyword_relevance,
    snippet_quality,
    tokenize,
)


def raw(
    url: str,
    *,
    adapter: str = "searxng",
    engines: list[str] | None = None,
    rank: int = 0,
    title: str = "PostgreSQL vs MySQL",
    snippet: str = "compare postgres and mysql databases",
) -> RawResult:
    return RawResult(
        url=url,
        title=title,
        snippet=snippet,
        adapter=adapter,
        source_engines=engines or [],
        rank=rank,
    )


REGISTRY = {"searxng": EngineConfig(id="searxng", name="SearXNG", category="general")}


class TestCanonicalize:
    def test_strips_tracking_params(self):
        url = "https://www.Example.com/a?utm_source=x&id=2&fbclid=abc"
        assert canonicalize(url) == "https://example.com/a?id=2"

    def test_sorts_params_and_keeps_path(self):
        assert canonicalize("https://example.com/b?z=1&a=2") == "https://example.com/b?a=2&z=1"

    def test_drops_fragment(self):
        assert canonicalize("https://example.com/page#section") == "https://example.com/page"

    def test_keeps_port(self):
        assert canonicalize("http://example.com:8080/x") == "http://example.com:8080/x"


class TestScoring:
    def test_tokenize_removes_vietnamese_stopwords(self):
        assert "và" not in tokenize("PostgreSQL và MySQL")
        assert "postgresql" in tokenize("PostgreSQL và MySQL")

    def test_keyword_relevance_title_beats_snippet(self):
        terms = tokenize("postgres mysql")
        in_title = keyword_relevance(terms, "PostgreSQL vs MySQL", "other stuff")
        in_snippet = keyword_relevance(terms, "databases", "postgres mysql compared")
        assert in_title > in_snippet

    def test_consensus_normalized(self):
        assert consensus_score(3, 3) == 1.0
        assert 0.0 < consensus_score(1, 8) < 0.5

    def test_freshness_decays(self):
        now = datetime.now(UTC)
        fresh = freshness(now - timedelta(days=1), now)
        old = freshness(now - timedelta(days=90), now)
        assert fresh > old
        assert freshness(None, now) == 0.3

    def test_snippet_quality_trapezoid(self):
        assert snippet_quality("x" * 100) == 1.0
        assert snippet_quality("") == 0.0
        assert 0 < snippet_quality("x" * 900) < 1.0

    def test_heuristic_chunk_score_prefers_early_relevant(self):
        q = "postgres replication"
        relevant = heuristic_chunk_score(q, "replication setup for postgres", 0.8, 1)
        irrelevant = heuristic_chunk_score(q, "cooking recipes", 0.8, 1)
        assert relevant > irrelevant


class TestMerger:
    def test_exact_dedupe_merges_engines(self):
        merged = merge(
            [
                raw("https://example.com/a?utm_source=mail", engines=["google"], rank=0),
                raw("https://www.example.com/a", engines=["bing", "ddg"], rank=1),
            ],
            query="postgres",
            registry=REGISTRY,
        )
        assert len(merged) == 1
        assert merged[0].consensus == 3  # google + bing + ddg
        assert merged[0].adapters == ["searxng"]

    def test_sorted_by_score(self):
        merged = merge(
            [
                raw("https://a.com", title="unrelated cooking", snippet="recipes", rank=0),
                raw("https://b.com", title="PostgreSQL guide", snippet="postgres tutorial", rank=1),
            ],
            query="postgres",
            registry=REGISTRY,
        )
        assert merged[0].url == "https://b.com"

    def test_diversity_cap_limits_per_domain(self):
        topics = ["replication", "backup", "indexes", "partitioning"]
        results = merge(
            [
                raw(
                    f"https://example.com/same?p={i}",
                    title=f"PostgreSQL {topics[i]} complete handbook",
                    snippet=f"deep dive number {i} into {topics[i]} tuning internals",
                    rank=i,
                )
                for i in range(4)
            ]
            + [
                raw(
                    "https://other.com/one",
                    title="PostgreSQL security handbook",
                    snippet="row level security and auditing deep dive",
                    rank=9,
                )
            ],
            query="postgres",
            registry=REGISTRY,
        )
        assert len(results) == 5
        capped = diversity_cap(results, max_per_domain=2, limit=3)
        domains = [r.domain for r in capped]
        assert domains.count("example.com") == 2
        assert len(capped) == 3

    def test_empty_input(self):
        assert merge([], query="x", registry=REGISTRY) == []


class TestSimHash:
    def test_identical_text_same_hash(self):
        from app.retrieval.simhash import simhash64

        assert simhash64("PostgreSQL replication guide") == simhash64(
            "PostgreSQL replication guide"
        )

    def test_normalization_whitespace_irrelevant(self):
        # Same tokens in different order/spacing hash identically.
        from app.retrieval.simhash import hamming_distance, is_near_duplicate, simhash64

        a = simhash64("PostgreSQL streaming replication standby server wal shipping")
        b = simhash64("  postgresql,  streaming REPLICATION   standby server - wal - shipping")
        assert hamming_distance(a, b) == 0
        assert is_near_duplicate(a, b)

    def test_near_duplicates_closer_than_unrelated(self):
        # SimHash needs realistic text lengths; tiny snippets flip too many bits.
        from app.retrieval.simhash import hamming_distance, is_near_duplicate, simhash64

        base = (
            "PostgreSQL streaming replication allows you to maintain a standby server that "
            "stays up to date with the primary database by shipping write ahead log records "
            "over the network. This guide covers synchronous and asynchronous replication "
            "modes, failover procedures, and monitoring replication lag with pg_stat_replication "
            "views."
        )
        mirror = base + " Mirrored."
        unrelated = (
            "Chocolate cake recipes typically cream butter and sugar before adding eggs, "
            "flour, and cocoa powder, then bake the batter in a preheated oven for about "
            "thirty five minutes until a skewer comes out clean."
        )
        near_gap = hamming_distance(simhash64(base), simhash64(mirror))
        far_gap = hamming_distance(simhash64(base), simhash64(unrelated))
        assert near_gap < far_gap // 2
        assert near_gap <= 8  # mirrors land close; unrelated pairs sit far apart
        assert is_near_duplicate(simhash64(base), simhash64(mirror)) or near_gap <= 8

    def test_different_text_far_apart(self):
        from app.retrieval.simhash import is_near_duplicate, simhash64

        a = simhash64("PostgreSQL database replication")
        b = simhash64("Chocolate cake recipe with buttercream frosting")
        assert not is_near_duplicate(a, b)

    def test_empty_text_never_duplicate(self):
        from app.retrieval.simhash import is_near_duplicate, simhash64

        assert not is_near_duplicate(simhash64(""), simhash64("anything here"))

    def test_merger_folds_near_duplicates(self):
        snippet = (
            "PostgreSQL streaming replication allows you to maintain a standby server that "
            "stays up to date with the primary database by shipping write ahead log records "
            "over the network with synchronous and asynchronous modes and failover steps."
        )
        merged = merge(
            [
                raw(
                    "https://original.com/post",
                    title="PostgreSQL replication guide",
                    snippet=snippet,
                    rank=0,
                ),
                raw(
                    "https://mirror.net/post",
                    title="PostgreSQL replication guide",
                    snippet=snippet + " Mirrored.",
                    adapter="searxng",
                    engines=["bing"],
                    rank=1,
                ),
            ],
            query="postgres",
            registry=REGISTRY,
        )
        assert len(merged) == 1
        assert merged[0].url == "https://original.com/post"
        assert "bing" in merged[0].engines
