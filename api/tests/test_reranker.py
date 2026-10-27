"""Reranker tests: heuristic identity, blending, graceful fallback."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.extraction.chunker import Chunk
from app.retrieval.reranker import (
    HeuristicReranker,
    JinaAPIReranker,
    ONNXReranker,
    RerankerUnavailable,
    build_reranker,
    heuristic_order,
)


def chunk(content: str) -> Chunk:
    return Chunk(
        id="c" * 32,
        document_url="https://x.com",
        position=1,
        heading_path="",
        content=content,
        word_count=len(content.split()),
        source_title="T",
    )


def scored(*contents_scores: tuple[str, float]) -> list[tuple[float, Chunk]]:
    return [(score, chunk(content)) for content, score in contents_scores]


async def test_heuristic_keeps_score_order():
    chunks = await HeuristicReranker().rerank(
        "postgres", scored(("weak", 0.1), ("strong", 0.9), ("middle", 0.5))
    )
    assert [c.content for c in chunks] == ["strong", "middle", "weak"]


def test_heuristic_order_helper():
    chunks = heuristic_order(scored(("b", 0.2), ("a", 0.8)))
    assert [c.content for c in chunks] == ["a", "b"]


async def test_jina_rerank_blends_model_scores():
    payload = {
        "results": [
            {"index": 0, "relevance_score": 1.0},  # weak heuristic, perfect model
            {"index": 1, "relevance_score": 0.0},
            {"index": 2, "relevance_score": 0.3},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/rerank"
        assert request.headers["authorization"] == "Bearer jina-key"
        return httpx.Response(200, json=payload)

    reranker = JinaAPIReranker("jina-key")
    import app.retrieval.reranker as rr

    # Inject a mocked HTTP transport for the client created inside rerank().
    original_asyncclient = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_asyncclient(*args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(rr.httpx, "AsyncClient", make_client)
    try:
        chunks = await reranker.rerank("q", scored(("weak", 0.1), ("strong", 0.9), ("middle", 0.5)))
    finally:
        monkey.undo()
    # Model score 1.0 dominates the 0.1 heuristic for the first document.
    assert chunks[0].content == "weak"


async def test_jina_failure_falls_back(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    import app.retrieval.reranker as rr

    original_asyncclient = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_asyncclient(*args, **kwargs)

    monkeypatch.setattr(rr.httpx, "AsyncClient", make_client)
    reranker = JinaAPIReranker("jina-key")
    input_order = scored(("strong", 0.9), ("weak", 0.1))
    with pytest.raises(RerankerUnavailable):
        await reranker.rerank("q", input_order)


def test_build_reranker_factory():
    settings = Settings(ENVIRONMENT="test")
    assert build_reranker(settings).name == "heuristic"  # default

    settings_api = Settings(ENVIRONMENT="test", RERANKER="api", JINA_API_KEY="k")
    assert build_reranker(settings_api).name == "api"

    # api without key -> heuristic
    settings_nokey = Settings(ENVIRONMENT="test", RERANKER="api")
    assert build_reranker(settings_nokey).name == "heuristic"

    settings_onnx = Settings(ENVIRONMENT="test", RERANKER="onnx")
    assert build_reranker(settings_onnx).name == "onnx"


async def test_onnx_missing_dir_raises_unavailable():
    reranker = ONNXReranker(model_dir="")
    with pytest.raises(RerankerUnavailable):
        await reranker.rerank("q", scored(("a", 1.0)))


def test_onnx_load_failure_does_not_retry_forever(tmp_path):
    reranker = ONNXReranker(model_dir=str(tmp_path))  # dir exists, no model files
    with pytest.raises(RerankerUnavailable):
        reranker._ensure_loaded()  # sync method
    # The cached failure re-raises on every later attempt.
    with pytest.raises(RerankerUnavailable):
        reranker._ensure_loaded()
