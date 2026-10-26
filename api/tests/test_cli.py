"""CLI tests: parser + output formatting against a fake HTTP layer."""

from __future__ import annotations

import json

from app.cli import build_parser, main


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_version(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "metasearch" in capsys.readouterr().out


def test_search_pretty_output(monkeypatch, capsys):
    payload = {
        "results": [
            {
                "rank": 1,
                "title": "PostgreSQL docs",
                "url": "https://postgresql.org/docs",
                "score": 0.87,
                "found_by": ["searxng"],
                "snippet": "Official documentation",
                "published_at": "2025-01-15T00:00:00+00:00",
            }
        ],
        "engines_used": ["searxng"],
        "engines_failed": [],
        "latency_ms": 900,
    }
    seen: dict = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return FakeResponse(payload)

    monkeypatch.setattr("app.cli.httpx.post", fake_post)
    code = main(["--api-url", "http://api.test", "search", "postgres", "--lang", "vi", "-k", "5"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1. PostgreSQL docs" in out
    assert "(2025-01-15)" in out
    assert seen["url"] == "http://api.test/v1/search"
    assert seen["json"]["lang"] == "vi"
    assert seen["json"]["k"] == 5


def test_search_json_output(monkeypatch, capsys):
    payload = {"results": [], "engines_used": [], "engines_failed": [], "latency_ms": 1}

    monkeypatch.setattr("app.cli.httpx.post", lambda *a, **kw: FakeResponse(payload))
    code = main(["search", "q", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data == payload


def test_http_error_prints_message(monkeypatch, capsys):
    import httpx

    def fake_post(url, json=None, timeout=None):
        request = httpx.Request("POST", url)
        response = httpx.Response(503, json={"error": {"message": "no results"}}, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr("app.cli.httpx.post", fake_post)
    code = main(["search", "q"])
    assert code == 1
    assert "no results" in capsys.readouterr().err


def test_parser_requires_command():
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args(["search"])  # missing query
