"""Provider-agnostic LLM gateway (docs/02-system-architecture.md, LLM Gateway).

Phase 1 implements the two streaming providers MetaSearch needs:
- OpenAI-compatible ``/chat/completions`` (api.openai.com, vLLM, Ollama)
- Anthropic ``/v1/messages``

Streaming yields ``StreamDelta`` so SSE can forward tokens as they arrive and
still record token usage + estimated cost afterwards.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.core.logging import get_logger
from app.core.metrics import LLM_COST, LLM_TOKENS

logger = get_logger(__name__)

# USD per 1M tokens (input, output). Unknown models cost-estimate at 0.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
}


class LLMError(Exception):
    pass


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def cost_usd(self, model: str) -> float:
        prices = _PRICE_TABLE.get(model)
        if prices is None:
            # Prefix match so dated snapshots ("-2024-07-18") still price.
            prices = next(
                (v for k, v in _PRICE_TABLE.items() if model.startswith(k)),
                None,
            ) or (0.0, 0.0)
        return (self.input_tokens / 1e6 * prices[0]) + (self.output_tokens / 1e6 * prices[1])


@dataclass(slots=True)
class StreamDelta:
    text: str | None = None
    usage: Usage | None = None  # set on the final delta only


async def _sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON payloads from an httpx SSE line stream (``data: {...}``)."""
    async for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("llm_sse_bad_json", preview=data[:120])


class LLMGateway:
    """Single entrypoint used by the synthesizer; provider chosen from settings."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def provider(self) -> str:
        return self._settings.llm_provider

    def _require_provider(self) -> None:
        if self._settings.llm_provider == "none":
            raise LLMError(
                "LLM provider is 'none': set LLM_PROVIDER plus an API key to use /v1/answer"
            )

    def _headers(self, model: str) -> dict[str, str]:
        if self._settings.llm_provider == "anthropic":
            return {
                "x-api-key": self._settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {"Authorization": f"Bearer {self._settings.openai_api_key}"}

    def _body(
        self, messages: list[dict[str, str]], model: str, *, max_tokens: int
    ) -> dict[str, Any]:
        temperature = self._settings.llm_temperature
        if self._settings.llm_provider == "anthropic":
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            return {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "stream": True,
                "messages": [m for m in messages if m["role"] != "system"],
            }
        return {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": messages,
        }

    def _url(self) -> str:
        if self._settings.llm_provider == "anthropic":
            return f"{self._settings.anthropic_base_url.rstrip('/')}/messages"
        return f"{self._settings.openai_base_url.rstrip('/')}/chat/completions"

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream answer deltas; the last delta carries usage for metering."""
        self._require_provider()
        settings = self._settings
        body = self._body(messages, model, max_tokens=max_tokens or settings.llm_answer_max_tokens)
        async with self._stream_response(body, self._headers(model)) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode(errors="replace")[:500]
                raise LLMError(f"LLM HTTP {resp.status_code}: {detail}")
            async for payload in _sse_payloads(resp.aiter_lines()):
                delta, usage = _parse_delta(payload, settings.llm_provider)
                if delta is not None or usage is not None:
                    yield StreamDelta(text=delta, usage=usage)

    @asynccontextmanager
    async def _stream_response(
        self, body: dict[str, Any], headers: dict[str, str]
    ) -> AsyncIterator[httpx.Response]:
        url = self._url()
        if self._client is not None:
            async with self._client.stream("POST", url, json=body, headers=headers) as resp:
                yield resp
            return
        async with (
            httpx.AsyncClient(timeout=self._settings.request_timeout_s) as client,
            client.stream("POST", url, json=body, headers=headers) as resp,
        ):
            yield resp


def _parse_delta(payload: dict[str, Any], provider: str) -> tuple[str | None, Usage | None]:
    """Extract (text, usage) from one SSE payload; either side may be absent."""
    if provider == "anthropic":
        return _parse_anthropic(payload)
    return _parse_openai(payload)


def _parse_openai(payload: dict[str, Any]) -> tuple[str | None, Usage | None]:
    usage = None
    if payload.get("usage"):
        u = payload["usage"]
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
        )
    choices = payload.get("choices") or []
    text = None
    if choices:
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
    return text, usage


def _parse_anthropic(payload: dict[str, Any]) -> tuple[str | None, Usage | None]:
    event_type = payload.get("type")
    if event_type == "content_block_delta":
        text = (payload.get("delta") or {}).get("text")
        return text, None
    if event_type == "message_start":
        u = (payload.get("message") or {}).get("usage") or {}
        return None, Usage(input_tokens=int(u.get("input_tokens") or 0), output_tokens=0)
    if event_type == "message_delta":
        u = payload.get("usage") or {}
        return None, Usage(input_tokens=0, output_tokens=int(u.get("output_tokens") or 0))
    return None, None


def record_usage(model: str, usage: Usage) -> None:
    """Export token + cost metrics for one completion."""
    LLM_TOKENS.labels(model=model, direction="input").inc(usage.input_tokens)
    LLM_TOKENS.labels(model=model, direction="output").inc(usage.output_tokens)
    cost = usage.cost_usd(model)
    if cost > 0:
        LLM_COST.labels(model=model).inc(cost)
