"""Regression tests for the 2026-11-22 leftover-bug sweep.

Covers three live-seen defects:
1. Careers adapter spliced free-form queries into hostnames (IDNA crash on
   every fan-out) and accepted URL queries — an SSRF bypass because careers
   has no resolve_and_check layer, unlike /v1/scrape.
2. Windows Lite logging crashed with UnicodeEncodeError (cp1252 console) on
   any Vietnamese log line — "  --- Logging error ---" spam in .lite.err.log.
3. /v1/scrape rejected the natural single-URL body {"url": "..."} — fold it
   into the canonical {"urls": [...]} shape instead.
"""

from __future__ import annotations

import logging

import pytest

from app.api.v1.schemas import ScrapeRequest
from app.retrieval.adapters.careers import CareersAdapter

# --- 1. Careers adapter: no query-in-hostname, no query-as-URL -------------


class TestCareersUrlSafety:
    def test_known_company_maps_to_curated_url(self):
        adapter = CareersAdapter()
        assert adapter._get_careers_url("openai careers page") == ("https://openai.com/careers")

    def test_ascii_slug_guesses_pattern_url(self):
        adapter = CareersAdapter()
        assert adapter._get_careers_url("acmecorp") == "https://acmecorp.com/careers"

    def test_multilingual_prose_query_returns_none(self):
        # Live crasher 2026-11-21: this query hit httpx as a hostname
        # '"hqc" công bố ... .com' → InvalidCodepoint/IDNA.
        adapter = CareersAdapter()
        assert adapter._get_careers_url('"hqc" công bố thông tin nghị quyết HĐQT') is None

    def test_url_query_is_never_fetched(self):
        # SSRF: a query that *is* a URL must not become the fetch target
        # (careers has no SSRF validator; user URLs belong to /v1/scrape).
        adapter = CareersAdapter()
        assert adapter._get_careers_url("http://192.168.0.1:6379/") is None
        assert adapter._get_careers_url("https://169.254.169.254/latest") is None

    def test_mixed_slug_with_spaces_returns_none(self):
        adapter = CareersAdapter()
        assert adapter._get_careers_url("openai research") is not None  # known word
        assert adapter._get_careers_url("two words") is None
        assert adapter._get_careers_url("funny/slash") is None
        assert adapter._get_careers_url("a" * 40) is None  # slug cap

    @pytest.mark.asyncio
    async def test_search_with_unsafe_query_is_a_graceful_no_op(self):
        # Before the fix this raised httpx.InvalidURL through the engine fan-out.
        adapter = CareersAdapter()
        assert await adapter.search('"hqc" công bố thông tin nghị quyết HĐQT') == []


# --- 2. UTF-8 stdio reconfiguration (Windows cp1252 consoles) --------------


class TestUtf8Stdio:
    def test_configure_logging_reconfigures_streams(self, monkeypatch):
        from app.core import logging as ms_logging

        calls: list[dict[str, str]] = []

        class FakeStream:
            def reconfigure(self, *, encoding: str, errors: str) -> None:
                calls.append({"encoding": encoding, "errors": errors})

        monkeypatch.setattr(ms_logging.sys, "stdout", FakeStream(), raising=False)
        monkeypatch.setattr(ms_logging.sys, "stderr", FakeStream(), raising=False)
        # Reset the one-shot guard so configure_logging exercises the streams.
        monkeypatch.setattr(ms_logging, "_configured", False)

        # Basic config on a real stream would double-attach handlers; match
        # the production call and tolerate the settings load.
        ms_logging.configure_logging()

        assert calls, "expected stdout/stderr reconfigure() calls"
        assert all(c["encoding"] == "utf-8" and c["errors"] == "replace" for c in calls)

    def test_configure_logging_survives_unreconfigurable_streams(self, monkeypatch):
        from app.core import logging as ms_logging

        class DeadStream:
            def reconfigure(self, **kwargs):
                raise ValueError("cannot reconfigure")

        monkeypatch.setattr(ms_logging.sys, "stdout", DeadStream(), raising=False)
        monkeypatch.setattr(ms_logging.sys, "stderr", DeadStream(), raising=False)
        monkeypatch.setattr(ms_logging, "_configured", False)

        ms_logging.configure_logging()  # must not raise
        assert logging.getLogger().handlers  # logging still configured


# --- 3. /v1/scrape accepts the single-URL convenience body -----------------


class TestScrapeRequestUrlAlias:
    def test_canonical_urls_shape_unchanged(self):
        req = ScrapeRequest(urls=["https://example.com/a", "https://example.com/b"])
        assert req.urls == ["https://example.com/a", "https://example.com/b"]
        assert req.url is None

    def test_single_url_alias_folds_into_urls(self):
        req = ScrapeRequest(url="https://example.com/one")
        assert req.urls == ["https://example.com/one"]

    def test_urls_wins_when_both_given(self):
        req = ScrapeRequest(urls=["https://example.com/keep"], url="https://example.com/drop")
        assert req.urls == ["https://example.com/keep"]

    def test_alias_respects_list_bounds(self):
        with pytest.raises(ValueError):
            ScrapeRequest()
        with pytest.raises(ValueError):
            ScrapeRequest(urls=[])
        with pytest.raises(ValueError):
            ScrapeRequest(url="")
