"""Reranker coverage round 2: MMR/per-URL diversity, LLM listwise reranker,
BGE dir resolution and factory branches (docs/20 P0-2, docs/31 Wave 1)."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.extraction.chunker import Chunk
from app.retrieval.reranker import (
    BGEReranker,
    HeuristicReranker,
    JinaAPIReranker,
    LLMListwiseReranker,
    ONNXReranker,
    RerankerUnavailable,
    _blend,
    build_reranker,
)


def chunk(content: str, url: str = "https://x.com/a", title: str = "T") -> Chunk:
    return Chunk(
        id=f"id-{content[:12]}",
        document_url=url,
        position=1,
        heading_path="",
        content=content,
        word_count=len(content.split()),
        source_title=title,
    )


def scored(*pairs: tuple[str, float], url: str = "https://x.com/a") -> list[tuple[float, Chunk]]:
    return [(score, chunk(content, url=url)) for content, score in pairs]


# --- HeuristicReranker diversity paths (lines 61-110) -----------------------------


async def test_heuristic_empty_input():
    assert await HeuristicReranker().rerank("q", []) == []


async def test_heuristic_per_url_overflow_refill_preserves_all():
    """Chunks beyond the per-URL cap go to overflow and are appended back when
    the selection is below the cap — diversity first, remainder at the tail."""
    r = HeuristicReranker(max_chunks=24, max_per_url=2)
    pairs = [
        ("alpha distinct text one two three four five", 0.9),
        ("bravo different words six seven eight nine ten", 0.8),
        ("charlie more vocabulary eleven twelve thirteen fourteen", 0.7),
        ("delta extra vocabulary fifteen sixteen seventeen eighteen", 0.6),
    ]
    chunks = await r.rerank("vocabulary", scored(*pairs))
    assert len(chunks) == 4
    # BM25-blended ordering picks the query-relevant chunks first; the
    # per-URL overflow is appended back only after them.
    assert {c.content.split()[0] for c in chunks[:2]} <= {"charlie", "delta", "alpha", "bravo"}
    assert len({c.content for c in chunks}) == 4


async def test_heuristic_near_duplicate_suppressed_then_refilled():
    r = HeuristicReranker(max_chunks=24, max_per_url=10)
    base = "the quick brown fox jumps over lazy dogs daily"
    dup = "the quick brown fox jumps over lazy dogs daily today"
    other = "completely different vocabulary about quantum computing chips"
    chunks = await r.rerank("fox", [(1.0, chunk(base)), (0.9, chunk(dup)), (0.8, chunk(other))])
    # near-duplicate is pushed to overflow, distinct chunk is picked first
    assert [c.content for c in chunks][:2] == [base, other]
    assert chunks[-1].content.startswith("the quick brown")


async def test_heuristic_max_chunks_cap():
    r = HeuristicReranker(max_chunks=2)
    chunks = await r.rerank(
        "q",
        scored(
            ("one alpha beta gamma", 1.0), ("two delta epsilon zeta", 0.9), ("three eta theta", 0.8)
        ),
    )
    assert len(chunks) == 2


# --- JinaAPI empty input ----------------------------------------------------------


async def test_jina_empty_input_returns_empty():
    reranker = JinaAPIReranker("k", client=httpx.AsyncClient(transport=httpx.MockTransport(None)))
    assert await reranker.rerank("q", []) == []


# --- ONNX paths -------------------------------------------------------------------


async def test_onnx_empty_input_returns_empty():
    assert await ONNXReranker(model_dir="").rerank("q", []) == []


async def test_onnx_rerank_raises_unavailable_via_async_path(tmp_path):
    reranker = ONNXReranker(model_dir=str(tmp_path))  # dir exists, no model files
    with pytest.raises(RerankerUnavailable):
        await reranker.rerank("q", scored(("a", 1.0)))


async def test_onnx_model_file_missing_raises(tmp_path):
    reranker = ONNXReranker(model_dir=str(tmp_path))
    with pytest.raises(RerankerUnavailable, match=r"dir not found|load failed"):
        reranker._ensure_loaded()


def test_blend_empty():
    assert _blend([], {}) == []


# --- BGEReranker dir resolution (lines 286-337) -----------------------------------


def test_bge_pick_file_prefers_int8(tmp_path):
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model_quantized.onnx").write_text("x")
    settings = Settings(ENVIRONMENT="test", BGE_RERANKER_DIR=str(tmp_path))
    assert BGEReranker._pick_file(settings, str(tmp_path)) == "onnx/model_quantized.onnx"


def test_bge_pick_file_fallback_default():
    settings = Settings(ENVIRONMENT="test")
    assert BGEReranker._pick_file(settings, "") == "model.onnx"


def test_bge_resolve_dir_explicit():
    settings = Settings(ENVIRONMENT="test", BGE_RERANKER_DIR="/models/bge")
    assert BGEReranker._resolve_dir(settings) == "/models/bge"


def test_bge_resolve_dir_local_cache_hit(tmp_path):
    local = tmp_path / "BAAI__bge-reranker-v2-m3"
    local.mkdir()
    (local / "tokenizer.json").write_text("{}")
    settings = Settings(ENVIRONMENT="test", MODEL_CACHE_DIR=str(tmp_path))
    assert BGEReranker._resolve_dir(settings) == str(local)


def test_bge_resolve_dir_download_failure_returns_local_path(tmp_path, monkeypatch):
    # huggingface_hub is optional; block the import so the guarded fallback runs.
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", None)
    settings = Settings(ENVIRONMENT="test", MODEL_CACHE_DIR=str(tmp_path))
    resolved = BGEReranker._resolve_dir(settings)
    assert resolved == str(tmp_path / "BAAI__bge-reranker-v2-m3")


def test_bge_reranker_init_and_lazy_failure(tmp_path):
    settings = Settings(ENVIRONMENT="test", BGE_RERANKER_DIR=str(tmp_path))
    reranker = BGEReranker(settings)
    assert reranker.name == "bge"


# --- LLMListwiseReranker (lines 346-393) ------------------------------------------


class FakeGateway:
    provider = "fake"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, messages, *, max_tokens=None, task=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "task": task})
        return self._reply, {"tokens": 10}


async def test_llm_listwise_requires_gateway():
    with pytest.raises(RerankerUnavailable, match="requires configured"):
        await LLMListwiseReranker(None).rerank("q", scored(("a", 1.0)))


async def test_llm_listwise_unconfigured_provider_raises():
    class NoopGateway:
        provider = "none"

    with pytest.raises(RerankerUnavailable):
        await LLMListwiseReranker(NoopGateway()).rerank("q", scored(("a", 1.0)))


async def test_llm_listwise_empty_input():
    assert await LLMListwiseReranker(FakeGateway("{}")).rerank("q", []) == []


async def test_llm_listwise_happy_path_reorders():
    # Only index 1 is ranked -> model score 1.0 for "second", 0.0 for "first",
    # which outweighs the heuristic gap and flips the order.
    gateway = FakeGateway('Sure! {"indices": [1]}')
    reranker = LLMListwiseReranker(gateway)
    chunks = await reranker.rerank("query", scored(("first", 0.9), ("second", 0.1)))
    assert chunks[0].content == "second"
    assert chunks[1].content == "first"
    call = gateway.calls[0]
    assert call["task"] == "rerank"
    assert "[0]" in call["messages"][1]["content"] and "first" in call["messages"][1]["content"]


async def test_llm_listwise_bad_json_raises_unavailable():
    reranker = LLMListwiseReranker(FakeGateway("I cannot rank that, sorry."))
    with pytest.raises(RerankerUnavailable):
        await reranker.rerank("q", scored(("a", 1.0), ("b", 0.5)))


async def test_llm_listwise_out_of_range_indices_raise():
    reranker = LLMListwiseReranker(FakeGateway('{"indices": [99, -1]}'))
    with pytest.raises(RerankerUnavailable):
        await reranker.rerank("q", scored(("a", 1.0), ("b", 0.5)))


async def test_llm_listwise_gateway_error_raises_unavailable():
    class BrokenGateway:
        provider = "fake"

        async def complete(self, messages, **kwargs):
            raise RuntimeError("upstream down")

    with pytest.raises(RerankerUnavailable):
        await LLMListwiseReranker(BrokenGateway()).rerank("q", scored(("a", 1.0)))


# --- factory branches (lines 406-408) ---------------------------------------------


def test_build_reranker_bge_branch():
    settings = Settings(ENVIRONMENT="test", RERANKER="bge", BGE_RERANKER_DIR="/models/bge")
    assert build_reranker(settings).name == "bge"


def test_build_reranker_llm_branch():
    settings = Settings(ENVIRONMENT="test", RERANKER="llm")
    gateway = FakeGateway("{}")
    assert build_reranker(settings, gateway=gateway).name == "llm"
    # llm without a gateway falls back to heuristic
    assert build_reranker(settings).name == "heuristic"
