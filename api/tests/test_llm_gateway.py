"""LLM gateway tests: OpenAI + Anthropic SSE parsing, usage metering."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.llm.gateway import LLMError, LLMGateway, Usage


def make_settings(provider: str, base_url: str) -> Settings:
    return Settings(
        ENVIRONMENT="test",
        LLM_PROVIDER=provider,
        OPENAI_API_KEY="sk-test",
        ANTHROPIC_API_KEY="anth-test",
        OPENAI_BASE_URL=base_url if provider == "openai" else "https://api.openai.com/v1",
        ANTHROPIC_BASE_URL=base_url if provider == "anthropic" else "https://api.anthropic.com/v1",
        LLM_TEMPERATURE="0.1",
    )


def sse_response(events: list[str]) -> httpx.Response:
    body = "".join(f"data: {e}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def collect(gen) -> tuple[str, Usage | None]:
    text = ""
    usage = Usage()
    async for delta in gen:
        if delta.text:
            text += delta.text
        if delta.usage:
            usage.input_tokens += delta.usage.input_tokens
            usage.output_tokens += delta.usage.output_tokens
    return text, usage


class TestOpenAI:
    async def test_stream_and_usage(self):
        events = [
            json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
            json.dumps({"choices": [{"delta": {"content": " world [1]"}}]}),
            json.dumps(
                {
                    "choices": [{"delta": {}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                }
            ),
            "[DONE]",
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["stream"] is True
            assert body["model"] == "gpt-4o-mini"
            assert request.headers["authorization"] == "Bearer sk-test"
            return sse_response(events)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(make_settings("openai", "http://llm.test/v1"), client=client)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        text, usage = await collect(gw.stream(messages, model="gpt-4o-mini"))
        assert text == "Hello world [1]"
        assert usage.input_tokens == 11
        assert usage.output_tokens == 4

    async def test_provider_none_raises(self):
        gw = LLMGateway(make_settings("none", "http://llm.test/v1"))
        with pytest.raises(LLMError):
            async for _ in gw.stream([{"role": "user", "content": "hi"}], model="m"):
                pass

    async def test_http_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(make_settings("openai", "http://llm.test/v1"), client=client)
        with pytest.raises(LLMError, match="429"):
            async for _ in gw.stream([{"role": "user", "content": "hi"}], model="gpt-4o-mini"):
                pass


class TestAnthropic:
    async def test_stream_and_usage(self):
        events = [
            json.dumps(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 20, "output_tokens": 0}},
                }
            ),
            json.dumps({"type": "content_block_delta", "delta": {"text": "Xin chào"}}),
            json.dumps({"type": "message_delta", "usage": {"output_tokens": 7}}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["system"] == "sys"
            assert [m["role"] for m in body["messages"]] == ["user"]
            assert request.headers["x-api-key"] == "anth-test"
            return sse_response(events)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(make_settings("anthropic", "http://llm.test/v1"), client=client)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        text, usage = await collect(gw.stream(messages, model="claude-3-5-haiku-latest"))
        assert text == "Xin chào"
        assert usage.input_tokens == 20
        assert usage.output_tokens == 7


class TestCost:
    def test_known_model(self):
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert usage.cost_usd("gpt-4o-mini") == pytest.approx(0.15 + 0.60)

    def test_dated_snapshot_matches_by_prefix(self):
        usage = Usage(input_tokens=1_000_000, output_tokens=0)
        assert usage.cost_usd("gpt-4o-mini-2024-07-18") == pytest.approx(0.15)

    def test_unknown_model_zero(self):
        assert Usage(1000, 1000).cost_usd("totally-unknown") == 0.0
