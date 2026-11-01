"""Deep edge-case and robustness tests for Multi-Endpoint LLM Council,
Anonymous Cross-Review, Dream-RSI Replay World, and JSON parser recovery.
"""

from __future__ import annotations

from app.llm.council import (
    EndpointStats,
    PanelistDraft,
    _compute_consensus_agreement,
    extract_json_object,
)


class TestJsonExtractorRobustness:
    def test_direct_clean_json(self) -> None:
        raw = '{"scores": {"Candidate A": 9.2}, "critique_summary": "Solid analysis"}'
        parsed = extract_json_object(raw)
        assert parsed is not None
        assert parsed["scores"]["Candidate A"] == 9.2

    def test_markdown_code_fence_json(self) -> None:
        raw = """Here is my review:
```json
{
  "reviews": [
    {"candidate_id": "Candidate A", "score": 8.0, "strengths": "Accurate [1]", "flaws": "None"},
    {"candidate_id": "Candidate B", "score": 0.0, "strengths": "", "flaws": "Hallucinated figures"}
  ]
}
```
Hope this is helpful!"""
        parsed = extract_json_object(raw)
        assert parsed is not None
        reviews = parsed["reviews"]
        assert len(reviews) == 2
        assert reviews[0]["score"] == 8.0
        assert reviews[1]["score"] == 0.0

    def test_preceding_and_trailing_braces_in_prose(self) -> None:
        raw = """Prefix text with some {curly note} here.
{"scores": {"Candidate A": 7.8, "Candidate B": 8.5}, "critique_summary": "Good"}
Trailing text with {another brace} at the end."""
        parsed = extract_json_object(raw)
        assert parsed is not None
        assert parsed["scores"]["Candidate A"] == 7.8
        assert parsed["scores"]["Candidate B"] == 8.5

    def test_nested_braces_inside_strings(self) -> None:
        raw = (
            '{"candidate_id": "Candidate A", '
            '"flaws": "Used invalid format {marker} here", "score": 6.5}'
        )
        parsed = extract_json_object(raw)
        assert parsed is not None
        assert parsed["candidate_id"] == "Candidate A"
        assert parsed["score"] == 6.5

    def test_empty_or_corrupt_returns_none(self) -> None:
        assert extract_json_object("") is None
        assert extract_json_object("   ") is None
        assert extract_json_object("Just plain prose without any json") is None
        assert extract_json_object("{corrupt json: not valid}") is None


class TestConsensusAgreementRobustness:
    def test_single_draft_returns_perfect_agreement(self) -> None:
        draft = PanelistDraft(
            candidate_id="Candidate A",
            provider="openrouter",
            model="free",
            answer_md="VCB CAMELS [1]",
            valid_citations=[1],
            invalid_citations=[],
            citation_precision=1.0,
            quantitative_density=0.8,
            source_coverage=1.0,
            deterministic_score=9.5,
            peer_score=9.5,
            composite_score=9.5,
            latency_ms=800,
        )
        assert _compute_consensus_agreement([draft]) == 1.0

    def test_pairwise_jaccard_overlap(self) -> None:
        d1 = PanelistDraft(
            candidate_id="Candidate A",
            provider="p1",
            model="m1",
            answer_md="Text [1][2]",
            valid_citations=[1, 2],
            invalid_citations=[],
            citation_precision=1.0,
            quantitative_density=0.5,
            source_coverage=0.5,
            deterministic_score=8.0,
            peer_score=8.0,
            composite_score=8.0,
            latency_ms=1000,
        )
        d2 = PanelistDraft(
            candidate_id="Candidate B",
            provider="p2",
            model="m2",
            answer_md="Text [1][3]",
            valid_citations=[1, 3],
            invalid_citations=[],
            citation_precision=1.0,
            quantitative_density=0.5,
            source_coverage=0.5,
            deterministic_score=8.0,
            peer_score=8.0,
            composite_score=8.0,
            latency_ms=1000,
        )
        score = _compute_consensus_agreement([d1, d2])
        assert 0.60 <= score <= 0.70


class TestUCBScoreStability:
    def test_untried_endpoint_gets_exploration_bonus(self) -> None:
        st = EndpointStats(endpoint_key="test:model", provider="test", model="model")
        assert st.ucb_score(total_trials=0) == 1.25
        assert st.ucb_score(total_trials=100) == 1.25

    def test_experienced_endpoint_score_bounded(self) -> None:
        st = EndpointStats(
            endpoint_key="test:model",
            provider="test",
            model="model",
            trials=50,
            successes=48,
            avg_citation_precision=0.95,
            avg_peer_score=8.8,
            avg_latency_ms=800.0,
        )
        sc = st.ucb_score(total_trials=200)
        assert 0.5 <= sc <= 2.5
