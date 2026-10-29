"""RobotsCache tests: parse, allow/deny, unreachable fallback, TTL."""

from __future__ import annotations

import httpx
import pytest

from app.extraction.robots import RobotsCache


def make_cache(handler) -> RobotsCache:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RobotsCache(client)


ROBOTS = "User-agent: *\nDisallow: /private/\nAllow: /\n"


async def test_disallowed_path_blocked():
    cache = make_cache(lambda r: httpx.Response(200, text=ROBOTS))
    allowed = await cache.allowed(
        "https://example.com/private/secret", user_agent="MetaSearchBot/1.0"
    )
    assert allowed is False  # falls into the * group


async def test_specific_agent_group_wins():
    robots = "User-agent: MetaSearchBot\nDisallow:\n\nUser-agent: *\nDisallow: /\n"
    cache = make_cache(lambda r: httpx.Response(200, text=robots))
    assert await cache.allowed("https://example.com/anything", user_agent="MetaSearchBot/1.0")
    assert not await cache.allowed("https://example.com/anything", user_agent="OtherBot/1.0")


async def test_allowed_path_passes():
    cache = make_cache(lambda r: httpx.Response(200, text=ROBOTS))
    allowed = await cache.allowed("https://example.com/public/page", user_agent="MetaSearchBot/1.0")
    assert allowed is True


async def test_missing_robots_means_allow():
    cache = make_cache(lambda r: httpx.Response(404, text="nope"))
    allowed = await cache.allowed("https://example.com/anything", user_agent="MetaSearchBot")
    assert allowed is True


async def test_unreachable_robots_means_allow():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    cache = make_cache(handler)
    allowed = await cache.allowed("https://example.com/anything", user_agent="MetaSearchBot")
    assert allowed is True


async def test_private_target_in_robots_check_is_guarded(monkeypatch):
    # robots fetch resolves DNS; a host resolving to a private IP must never
    # be fetched — resolve_and_check raises and the cache treats it as absent.
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=ROBOTS)

    cache = make_cache(handler)
    allowed = await cache.allowed("https://internal.example.com/page", user_agent="MetaSearchBot")
    assert allowed is True  # fail-open for the robots document itself
    assert calls["n"] == 0  # but nothing was fetched


async def test_robots_cached_per_origin():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=ROBOTS)

    cache = make_cache(handler)
    await cache.allowed("https://example.com/a", user_agent="MetaSearchBot")
    await cache.allowed("https://example.com/b", user_agent="MetaSearchBot")
    assert calls["n"] == 1  # one fetch, two checks
    cache.clear()
    await cache.allowed("https://example.com/c", user_agent="MetaSearchBot")
    assert calls["n"] == 2


@pytest.mark.parametrize("bad_origin", ["http://127.0.0.1", "ftp://x"])
async def test_pinned_robots_fail_open_without_fetching(bad_origin):
    # _fetch must never fetch an SSRF-forbidden origin; it fails open (None)
    # and the main fetch's resolve_and_check blocks the actual request.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=ROBOTS)

    cache = make_cache(handler)
    assert await cache._fetch(bad_origin) is None
    assert calls["n"] == 0
