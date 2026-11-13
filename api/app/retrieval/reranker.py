"""Rerank stage (docs/03-search-pipeline.md §5) — heuristic-first, 0ms default.

    heuristic   (mặc định) sort theo điểm đã tính, không load model nào
    onnx        jina-reranker-v2-base-multilingual int8 (~300MB, lazy load)
    api         Jina rerank API (~$0.001/query, chất lượng tốt nhất)

Mọi reranker có model đều fallback về thứ tự heuristic khi lỗi hoặc thiếu
dependency — rerank không bao giờ làm fail request.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

from app.config import Settings
from app.core.logging import get_logger
from app.core.offload import run_cpu
from app.http import get_http_client

if TYPE_CHECKING:
    from app.extraction.chunker import Chunk

logger = get_logger(__name__)

# Cross-encoder runs on the top chunks only (docs/03: top-30 → top-10).
RERANK_INPUT_CAP = 30
# Blend: model score dominates but heuristic order still matters on ties.
MODEL_WEIGHT = 0.5


class RerankerUnavailable(Exception):
    """Optional reranker could not run; caller keeps heuristic order."""


@runtime_checkable
class Reranker(Protocol):
    name: str

    async def rerank(self, query: str, scored: list[tuple[float, Chunk]]) -> list[Chunk]:
        """Return chunks best-first. ``scored[i][0]`` is the heuristic score."""


def heuristic_order(scored: list[tuple[float, Chunk]]) -> list[Chunk]:
    return [chunk for _, chunk in sorted(scored, key=lambda pair: -pair[0])]


class HeuristicReranker:
    """Default: reuse the scores computed during chunk selection (0ms)."""

    name = "heuristic"

    async def rerank(self, query: str, scored: list[tuple[float, Chunk]]) -> list[Chunk]:
        return heuristic_order(scored)


class JinaAPIReranker:
    """Jina rerank REST API (shares the JINA_API_KEY with the reader fallback)."""

    name = "api"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.jina.ai",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def rerank(self, query: str, scored: list[tuple[float, Chunk]]) -> list[Chunk]:
        if not scored:
            return []
        top = scored[:RERANK_INPUT_CAP]
        body = {
            "model": "jina-reranker-v2-base-multilingual",
            "query": query,
            "documents": [chunk.content for _, chunk in top],
        }
        try:
            client = self._client or get_http_client("internal")
            resp = await client.post(
                f"{self.base_url}/v1/rerank",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=5.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("rerank_api_failed", error=type(exc).__name__)
            raise RerankerUnavailable(str(exc)) from exc

        results = payload.get("results") or []
        model_scores = {int(r["index"]): float(r.get("relevance_score") or 0.0) for r in results}
        return _blend(top, model_scores)


class ONNXReranker:
    """Local int8 ONNX cross-encoder; model dir must hold model.onnx + tokenizer.json.

    Dependencies (onnxruntime, tokenizers) and the model itself are optional —
    any failure raises ``RerankerUnavailable`` and the caller keeps heuristic
    order (docs/12-roadmap-plan.md Phase 2).
    """

    name = "onnx"

    def __init__(self, model_dir: str, *, model_filename: str = "model.onnx") -> None:
        self._model_dir = model_dir
        self._model_filename = model_filename
        self._session = None
        self._tokenizer = None
        self._loaded = False
        self._load_error: Exception | None = None
        self._load_lock = asyncio.Lock()

    def _ensure_loaded(self) -> None:
        if self._load_error is not None:
            raise self._load_error
        if self._loaded:
            return
        try:
            self._load()
            self._loaded = True
        except RerankerUnavailable as exc:
            self._load_error = exc
            raise
        except Exception as exc:
            self._load_error = RerankerUnavailable(f"onnx reranker load failed: {exc}")
            raise self._load_error from exc

    def _load(self) -> None:
        import os

        if not self._model_dir or not os.path.isdir(self._model_dir):
            raise RerankerUnavailable(f"onnx reranker dir not found: {self._model_dir!r}")
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            model_path = os.path.join(self._model_dir, self._model_filename)
            if not os.path.exists(model_path):
                raise RerankerUnavailable(f"model file missing: {model_path}")
            tokenizer_path = os.path.join(self._model_dir, "tokenizer.json")
            self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            self._tokenizer = Tokenizer.from_file(tokenizer_path)
        except Exception as exc:
            raise RerankerUnavailable(f"onnx reranker load failed: {exc}") from exc

    async def rerank(self, query: str, scored: list[tuple[float, Chunk]]) -> list[Chunk]:
        if not scored:
            return []
        top = scored[:RERANK_INPUT_CAP]
        try:
            if not self._loaded:
                async with self._load_lock:
                    if not self._loaded:
                        await run_cpu(self._ensure_loaded)
            model_scores = await run_cpu(self._score, query, [chunk.content for _, chunk in top])
        except RerankerUnavailable:
            raise
        except Exception as exc:
            logger.warning("rerank_onnx_failed", error=type(exc).__name__)
            raise RerankerUnavailable(str(exc)) from exc
        return _blend(top, model_scores)

    def _score(self, query: str, documents: list[str]) -> dict[int, float]:
        import numpy as np

        if self._tokenizer is None or self._session is None:
            raise RerankerUnavailable("onnx reranker not loaded")
        encodings = self._tokenizer.encode_batch([(query, doc) for doc in documents])
        max_len = max(len(e.ids) for e in encodings)
        input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        attention = np.zeros((len(encodings), max_len), dtype=np.int64)
        token_type = np.zeros((len(encodings), max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            n = len(enc.ids)
            input_ids[i, :n] = enc.ids
            attention[i, :n] = enc.attention_mask
            token_type[i, :n] = getattr(enc, "type_ids", [0] * n)
        feed = {"input_ids": input_ids, "attention_mask": attention, "token_type_ids": token_type}
        available = {i.name for i in self._session.get_inputs()}
        raw = self._session.run(None, {k: v for k, v in feed.items() if k in available})[0]
        # Cross-encoder head emits logits; squash to 0..1 relevance.
        logits = np.asarray(raw).ravel()
        scores = 1.0 / (1.0 + np.exp(-logits))
        return {i: float(s) for i, s in enumerate(scores)}


def _blend(top: list[tuple[float, Chunk]], model_scores: dict[int, float]) -> list[Chunk]:
    """0.5 x heuristic + 0.5 x model, heuristic normalized against its max."""
    if not top:
        return []
    max_heuristic = max(score for score, _ in top) or 1.0
    blended = [
        (MODEL_WEIGHT * (score / max_heuristic) + MODEL_WEIGHT * model_scores.get(i, 0.0), i)
        for i, (score, _) in enumerate(top)
    ]
    blended.sort(key=lambda pair: -pair[0])
    return [top[i][1] for _, i in blended]


class BGEReranker(ONNXReranker):
    """docs/20 P0-2: BAAI/bge-reranker-v2-m3 cross-encoder, local ONNX (int8 preferred).

    Resolution order: settings.bge_reranker_dir -> snapshot_download of
    settings.bge_reranker_hf_repo into model_cache_dir (needs the optional
    huggingface_hub dep + network on first run). Any failure surfaces as
    RerankerUnavailable at first use and the caller keeps heuristic order —
    a missing model must never fail a request.
    """

    name = "bge"

    def __init__(self, settings: Settings) -> None:
        super().__init__(self._resolve_dir(settings), model_filename=self._pick_file(settings))

    @staticmethod
    def _pick_file(settings: Settings) -> str:
        # int8 quantized export is ~4x smaller and nearly lossless for reranking.
        import os

        local = settings.bge_reranker_dir
        if local and os.path.exists(os.path.join(local, "model_int8.onnx")):
            return "model_int8.onnx"
        return "model.onnx"

    @staticmethod
    def _resolve_dir(settings: Settings) -> str:
        import os

        if settings.bge_reranker_dir:
            return settings.bge_reranker_dir
        root = settings.model_cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "metasearch", "models"
        )
        local = os.path.join(root, settings.bge_reranker_hf_repo.replace("/", "__"))
        if os.path.isdir(local) and os.path.exists(os.path.join(local, "tokenizer.json")):
            return local
        try:
            from huggingface_hub import snapshot_download

            return snapshot_download(
                settings.bge_reranker_hf_repo,
                revision="main",  # nosec B615 - pinned branch; weights are public Apache-2.0
                local_dir=local,
                allow_patterns=[
                    "*.onnx",
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "config.json",
                    "special_tokens_map.json",
                ],
            )
        except Exception as exc:
            # Keep the path; ONNXReranker._load raises RerankerUnavailable lazily.
            logger.warning("bge_reranker_download_failed", error=type(exc).__name__)
            return local


def build_reranker(settings: Settings) -> Reranker:
    """Factory honoring ``RERANKER``; unusable options fall back to heuristic."""
    choice = settings.reranker
    if choice == "api" and settings.jina_api_key:
        return JinaAPIReranker(settings.jina_api_key, base_url=settings.reranker_api_base_url)
    if choice == "api":
        logger.warning("reranker_api_without_key", hint="set JINA_API_KEY")
    if choice == "onnx":
        return ONNXReranker(settings.onnx_reranker_dir)
    if choice == "bge":
        return BGEReranker(settings)
    return HeuristicReranker()
