"""Tests for the RAGAS-lite judge (docs/23 P1 #2): parse_judge_scores is
pure — no LLM, no network. llm_judge fail-open path verified via a fake."""

from __future__ import annotations

from app.eval_judge import llm_judge, parse_judge_scores


class TestParseJudgeScores:
    def test_valid_json_both_scores(self):
        out = parse_judge_scores('{"faithfulness": 0.85, "answer_relevance": 0.9}')
        assert out == {"faithfulness": 0.85, "answer_relevance": 0.9}

    def test_int_values_accepted(self):
        out = parse_judge_scores('{"faithfulness": 1, "answer_relevance": 0}')
        assert out == {"faithfulness": 1.0, "answer_relevance": 0.0}

    def test_out_of_range_nones(self):
        out = parse_judge_scores('{"faithfulness": 1.5, "answer_relevance": -0.1}')
        assert out["faithfulness"] is None
        assert out["answer_relevance"] is None

    def test_missing_key_nones(self):
        out = parse_judge_scores('{"faithfulness": 0.8}')
        assert out["answer_relevance"] is None

    def test_wrapped_in_prose(self):
        text = 'Here is my evaluation:\n{"faithfulness": 0.7, "answer_relevance": 0.6}\nDone.'
        out = parse_judge_scores(text)
        assert out["faithfulness"] == 0.7

    def test_no_json_nones(self):
        out = parse_judge_scores("I cannot evaluate this answer.")
        assert out["faithfulness"] is None
        assert out["answer_relevance"] is None

    def test_non_json_garbage(self):
        out = parse_judge_scores("```\nbroken{{{")
        assert out["faithfulness"] is None


class TestLlmJudgeFailOpen:
    async def test_gateway_raises_returns_nones(self, monkeypatch):
        class _Gw:
            async def complete(self, messages, **kw):
                raise RuntimeError("llm down")

        out = await llm_judge(_Gw(), "answer", "query", ["src"])
        assert out["faithfulness"] is None
        assert out["answer_relevance"] is None

    async def test_empty_answer_short_circuits(self):
        out = await llm_judge(None, "", "query", ["src"])
        assert out["faithfulness"] is None
        assert out["answer_relevance"] is None

    async def test_no_sources_short_circuits(self):
        out = await llm_judge(None, "answer", "query", [])
        assert out["faithfulness"] is None
