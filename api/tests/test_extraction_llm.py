"""SSRF guard, chunker and citation validation tests."""

from __future__ import annotations

import socket

import pytest

from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.ssrf import SSRFViolation, resolve_and_check
from app.llm.citations import validate_citations


class TestSSRF:
    async def test_blocks_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x.com"):
            with pytest.raises(SSRFViolation):
                await resolve_and_check(url)

    async def test_blocks_userinfo(self):
        with pytest.raises(SSRFViolation):
            await resolve_and_check("https://user:pass@example.com/")

    async def test_blocks_private_literal_ips(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"):
            with pytest.raises(SSRFViolation):
                await resolve_and_check(f"http://{ip}/x")

    async def test_blocks_ipv4_mapped_ipv6(self):
        with pytest.raises(SSRFViolation):
            await resolve_and_check("http://[::ffff:10.0.0.1]/x")

    async def test_blocks_private_dns(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.9", port or 80))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(SSRFViolation):
            await resolve_and_check("http://internal.example.com/")

    async def test_allows_public_literal_ip(self):
        ips = await resolve_and_check("https://93.184.216.34/x")
        assert ips == frozenset({"93.184.216.34"})


class TestChunker:
    def test_heading_paths_tracked(self):
        md = "# Intro\nintro text here\n\n## Setup\nsetup steps\n\n### Details\nmore details"
        chunks = chunk_markdown(md, document_url="https://x.com", source_title="T")
        assert chunks[0].heading_path == "Intro"
        assert chunks[1].heading_path == "Intro > Setup"
        assert chunks[2].heading_path == "Intro > Setup > Details"
        assert [c.position for c in chunks] == [1, 2, 3]

    def test_overlap_between_chunks(self):
        words = [f"w{i}" for i in range(800)]
        md = "\n\n".join([" ".join(words[i : i + 40]) for i in range(0, 800, 40)])
        chunks = chunk_markdown(md, document_url="https://x.com", source_title="T")
        assert len(chunks) >= 2
        # the 64-word overlap: chunk 2 starts with chunk 1's last 64 words
        assert chunks[0].content.split()[-64:] == chunks[1].content.split()[:64]

    def test_oversized_paragraph_windowed(self):
        md = " ".join(f"w{i}" for i in range(1500))
        chunks = chunk_markdown(md, document_url="https://x.com", source_title="T")
        assert all(c.word_count <= 512 for c in chunks)
        assert len(chunks) >= 3

    def test_empty_doc_falls_back_to_title(self):
        chunks = chunk_markdown("", document_url="https://x.com", source_title="Just a title")
        assert len(chunks) == 1
        assert chunks[0].content == "Just a title"
        assert chunks[0].source_title == "Just a title"


class TestCitations:
    def _chunks(self):
        c1 = Chunk(
            id="a" * 32,
            document_url="https://one.com",
            position=1,
            heading_path="",
            content="one",
            word_count=1,
            source_title="One",
        )
        c2 = Chunk(
            id="b" * 32,
            document_url="https://two.com",
            position=2,
            heading_path="",
            content="two",
            word_count=1,
            source_title="Two",
        )
        return [(1, c1), (2, c2)]

    def test_keeps_valid_strips_invalid(self):
        check = validate_citations("Claim one [1] and two [2]. Bogus [9].", self._chunks())
        assert "[9]" not in check.text
        assert "[1]" in check.text and "[2]" in check.text
        assert check.invalid_markers == [9]
        assert [c.n for c in check.citations] == [1, 2]
        assert check.citations[0].url == "https://one.com"
        assert check.citations[0].title == "One"

    def test_no_citations(self):
        check = validate_citations("nothing cited here", self._chunks())
        assert check.citations == []
        assert check.text == "nothing cited here"
