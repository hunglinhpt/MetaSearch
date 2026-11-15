"""Client-network helpers shared by the unauthenticated operational surfaces.

``/docs``, ``/openapi.json``, ``/metrics`` and the detailed part of ``/readyz``
are reachable without a key only from the machine itself. ``request.client.host``
alone is not proof of that: behind a reverse proxy on the same host every
caller shows up as ``127.0.0.1``. A request that carries forwarding headers was
relayed by something, so it is NOT treated as a direct local caller.
"""

from __future__ import annotations

from starlette.requests import Request

from app.config import get_settings

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Any of these means a proxy relayed the request, so the socket peer says
# nothing about the real caller.
FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


def is_direct_loopback(request: Request) -> bool:
    """True only for a caller connecting straight from this machine.

    The Starlette ``testclient`` pseudo-host counts only when ``ENVIRONMENT=test``;
    on a real deployment it is just an arbitrary string a proxy could not set,
    but there is no reason to keep it in the production allowlist.
    """
    host = request.client.host if request.client else ""
    local = host in LOOPBACK_HOSTS or (host == "testclient" and get_settings().is_test)
    if not local:
        return False
    return not any(name in request.headers for name in FORWARDING_HEADERS)
