"""Token counting tests: words default, tiktoken fallback semantics."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from app.config import Settings
from app.extraction.chunker import Chunk
from app.llm.prompts import build_context
from app.llm.tokens import make_counter


def chunk(text: str) -> Chunk:
    return Chunk(
        id="c" * 32,
        document_url="https://x.com",
        position=1,
        heading_path="",
        content=text,
        word_count=len(text.split()),
        source_title="T",
    )


class TestMakeCounter:
    def test_words_backend(self):
        settings = Settings(ENVIRONMENT="test", CONTEXT_TOKENIZER="words")
        counter = make_counter(settings)
        assert counter("one two three four") == 4

    def test_tiktoken_missing_package_falls_back_to_words(self, monkeypatch):
        settings = Settings(ENVIRONMENT="test", CONTEXT_TOKENIZER="tiktoken")
        # Simulate the optional dependency being absent.
        monkeypatch.setitem(sys.modules, "tiktoken", None)
        counter = make_counter(settings)
        assert counter is not None
        assert counter("one two three four") == 4  # words fallback

    def test_tiktoken_present_but_bpe_unavailable_falls_back(self, monkeypatch):
        settings = Settings(ENVIRONMENT="test", CONTEXT_TOKENIZER="tiktoken")

        class FakeTiktoken(ModuleType):
            @staticmethod
            def get_encoding(name):
                raise RuntimeError("offline: cannot download BPE file")

        monkeypatch.setitem(sys.modules, "tiktoken", FakeTiktoken("tiktoken"))
        counter = make_counter(settings)
        assert counter("one two three") == 3  # graceful words fallback

    def test_tiktoken_backend_when_available(self):
        pytest.importorskip("tiktoken")
        settings = Settings(ENVIRONMENT="test", CONTEXT_TOKENIZER="tiktoken")
        counter = make_counter(settings)
        # cl100k: these tokens are common enough that the count is stable
        # and generally lower than the word count for long English text.
        text = "The quick brown fox jumps over the lazy dog " * 20
        assert counter(text) >= 10
        assert counter("") == 0


class TestBuildContextBudget:
    def test_budget_limits_chunks(self):
        chunks = [chunk(" ".join(f"w{i}" for i in range(100))) for _ in range(5)]
        included = build_context(chunks, budget=250)  # words backend
        assert len(included) == 2  # 100+100 fits, third would exceed

    def test_first_chunk_always_included(self):
        big = chunk(" ".join(f"w{i}" for i in range(1000)))
        included = build_context([big], budget=10)
        assert len(included) == 1

    def test_token_counter_changes_packing(self):
        chunks = [chunk("one two three four five six seven eight"), chunk("x")]
        # words: 8 + 1 = 9 <= 9 budget → both fit
        assert len(build_context(chunks, budget=9)) == 2
        # a counter that overestimates → only first fits
        assert len(build_context(chunks, budget=9, count=lambda t: len(t) * 10)) == 1
