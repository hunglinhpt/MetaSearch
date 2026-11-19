"""Rerank stage (docs/03-search-pipeline.md §5) — heuristic-first, 0ms default.

    heuristic   (mặc định) sort theo điểm đã tính, không load model nào
    onnx        jina-reranker-v2-base-multilingual int8 (~300MB, lazy load)
    api         Jina rerank API (~$0.001/query, chất lượng tốt nhất)

Mọi reranker có model đều fallback về thứ tự heuristic khi lỗi hoặc thiếu
dependency — rerank không bao giờ làm fail request.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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
    """Hybrid BM25 + heuristic ranker with per-URL cap, MMR diversity, and 24-chunk limit."""

    name = "heuristic"

    def __init__(self, *, max_chunks: int = 24, max_per_url: int = 4) -> None:
        self.max_chunks = max_chunks
        self.max_per_url = max_per_url

    async def rerank(self, query: str, scored: list[tuple[float, Chunk]]) -> list[Chunk]:
        if not scored:
            return []
        from app.retrieval.hybrid import bm25_scores
        from app.retrieval.scoring import tokenize

        docs = [chunk.content for _, chunk in scored]
        bm25 = bm25_scores(query, docs)
        max_h = max((s for s, _ in scored), default=0.0) or 1.0
        max_b = max(bm25, default=0.0)
        combined: list[tuple[float, Chunk]] = []
        for (h_score, chunk), b_score in zip(scored, bm25, strict=True):
            nh = h_score / max_h
            nb = (b_score / max_b) if max_b > 0 else 0.0
            blended = 0.55 * nh + 0.45 * nb
            combined.append((blended, chunk))
        combined.sort(key=lambda pair: -pair[0])

        # Enforce per-URL diversity and greedy MMR duplicate suppression so downstream
        # compressors receive distinct evidence rather than 20 chunks from one boilerplate page.
        picked: list[Chunk] = []
        picked_tokens: list[set[str]] = []
        per_url: dict[str, int] = {}
        overflow: list[Chunk] = []
        for _score, chunk in combined:
            url_count = per_url.get(chunk.document_url, 0)
            if url_count >= self.max_per_url:
                overflow.append(chunk)
                continue
            toks = set(tokenize(chunk.content[:600]))
            is_near_dup = False
            if len(toks) >= 8:
                for pt in picked_tokens:
                    union = len(toks | pt)
                    if union > 0 and (len(toks & pt) / union) > 0.85:
                        is_near_dup = True
                        break
            if is_near_dup:
                overflow.append(chunk)
                continue
            per_url[chunk.document_url] = url_count + 1
            picked.append(chunk)
            if len(toks) >= 8:
                picked_tokens.append(toks)
            if len(picked) >= self.max_chunks:
                break
        if len(picked) < min(self.max_chunks, len(combined)) and overflow:
            for chunk in overflow:
                if chunk not in picked:
                    picked.append(chunk)
                    if len(picked) >= self.max_chunks:
                        break
        return picked


HybridBM25ChunkReranker = HeuristicReranker


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
            tok = Tokenizer.from_file(tokenizer_path)
            if hasattr(tok, "enable_truncation"):
                tok.enable_truncation(max_length=512)
            self._tokenizer = tok
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
        max_len = min(512, max((len(e.ids) for e in encodings), default=1))
        input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        attention = np.zeros((len(encodings), max_len), dtype=np.int64)
        token_type = np.zeros((len(encodings), max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            ids = enc.ids[:512]
            mask = enc.attention_mask[:512]
            tids = getattr(enc, "type_ids", [0] * len(ids))[:512]
            n = len(ids)
            input_ids[i, :n] = ids
            attention[i, :n] = mask
            token_type[i, :n] = tids
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
        model_dir = self._resolve_dir(settings)
        super().__init__(model_dir, model_filename=self._pick_file(settings, model_dir))

    @staticmethod
    def _pick_file(settings: Settings, resolved_dir: str = "") -> str:
        # int8 quantized export is ~4x smaller and nearly lossless for reranking.
        import os

        local = resolved_dir or settings.bge_reranker_dir
        if local:
            for candidate in (
                "model_int8.onnx",
                "onnx/model_int8.onnx",
                "onnx/model_quantized.onnx",
                "onnx/model.onnx",
                "model.onnx",
            ):
                if os.path.exists(os.path.join(local, candidate)):
                    return candidate
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


class LLMListwiseReranker:
    """Optional listwise LLM reranker over top chunks; fail-open to heuristic."""

    name = "llm"

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def rerank(self, query: str, scored: list[tuple[float, Chunk]]) -> list[Chunk]:
        if not scored:
            return []
        if self._gateway is None or getattr(self._gateway, "provider", "none") == "none":
            raise RerankerUnavailable("llm reranker requires configured LLM gateway")
        top = scored[: min(20, len(scored))]
        previews = "\n".join(
            f"[{i}] {(c.source_title or '')[:60]} | {c.content[:220].replace(chr(10), ' ')}"
            for i, (_s, c) in enumerate(top)
        )
        try:
            text, _usage = await asyncio.wait_for(
                self._gateway.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Rank the candidate passages by relevance to the query. "
                                'Reply ONLY with JSON {"indices": [best_idx, ...]}.'
                            ),
                        },
                        {"role": "user", "content": f"Query: {query}\n\nPassages:\n{previews}"},
                    ],
                    max_tokens=120,
                    task="rerank",
                ),
                timeout=4.0,
            )
            import json as _json
            import re as _re

            m = _re.search(r"\{[^{}]*indices[^{}]*\}", text, _re.DOTALL)
            if not m:
                raise ValueError("no indices json")
            indices = [int(i) for i in _json.loads(m.group(0)).get("indices", [])]
            model_scores: dict[int, float] = {}
            total = max(len(indices), 1)
            for rank_pos, idx in enumerate(indices):
                if 0 <= idx < len(top) and idx not in model_scores:
                    model_scores[idx] = 1.0 - (rank_pos / total)
            if not model_scores:
                raise ValueError("empty indices")
            return _blend(top, model_scores)
        except Exception as exc:
            logger.info("rerank_llm_fallback", error=type(exc).__name__)
            raise RerankerUnavailable(str(exc)) from exc


def build_reranker(settings: Settings, *, gateway: Any = None) -> Reranker:
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
    if choice == "llm" and gateway is not None:
        return LLMListwiseReranker(gateway)
    return HeuristicReranker()
