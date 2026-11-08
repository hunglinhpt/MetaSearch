"""Pydantic request/response models for v1 (docs/06-api-spec.md)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.llm.providers import LLMOverride


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    lang: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    k: int = Field(default=10, ge=1, le=50)
    engines: list[str] | None = None
    categories: list[str] = Field(default_factory=lambda: ["general"])
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    include_snippets: bool = True
    news: bool = False
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)
    # Keep or drop results by host; "example.com" also matches its subdomains. A filter that
    # removes every result returns complete=false, not a confirmed empty answer.
    include_domains: list[str] | None = Field(default=None, max_length=50)
    exclude_domains: list[str] | None = Field(default=None, max_length=50)


class CitationItem(BaseModel):
    n: int
    url: str
    title: str = ""
    chunk_id: str = ""


class AnswerRequest(SearchRequest):
    stream: bool = False
    model: str | None = Field(default=None, max_length=150)
    llm: LLMOverride | None = None
    council_mode: bool = False
    council_endpoints: list[LLMOverride | str] | None = None
    domain: str = Field(default="auto", max_length=60)


class ScrapeRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)
    timeout_s: float = Field(default=8.0, ge=1, le=30)
    max_chars: int = Field(default=200_000, ge=1, le=1_000_000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)


class ScrapeItem(BaseModel):
    url: str
    status: str  # ok | failed | blocked
    markdown: str = ""
    title: str = ""
    extractor_used: str = ""
    error: str | None = None
    truncated: bool = False


class SearchResponse(BaseModel):
    query_id: str
    results: list[dict[str, Any]]
    engines_used: list[str]
    engines_failed: list[str]
    engines_status: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int
    cached: bool = False
    complete: bool = True
    age_s: int | None = None


class AnswerResponse(BaseModel):
    answer_id: str
    answer_md: str
    citations: list[CitationItem] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    status: str = "complete"
    cached: bool = False
    complete: bool = True
    council: dict[str, Any] | None = None


def new_id() -> str:
    return str(uuid.uuid4())
