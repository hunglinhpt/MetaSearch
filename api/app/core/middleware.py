"""Pure-ASGI middleware.

Deliberately *not* ``BaseHTTPMiddleware``: that implementation buffers the
response body and breaks Server-Sent Events streaming.
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS, INFLIGHT_REQUESTS

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = b"x-request-id"


def _route_class_of(path: str) -> str:
    """Bound route label: the first path segment, never the raw path.

    ``/v1/search`` -> ``/v1``, ``/v1/queries/abc`` -> ``/v1``; cardinally
    bounded because the set of top-level segments is fixed.
    """
    parts = [p for p in (path or "").split("/") if p]
    if not parts:
        return "/"
    return "/" + parts[0]


class RequestContextMiddleware:
    """Assign a request id, expose it to logs, echo it in response, and record HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = str(scope.get("method") or "GET").upper()
        status_code = 500

        incoming = dict(scope.get("headers") or []).get(REQUEST_ID_HEADER)
        request_id = uuid.uuid4().hex
        if incoming:
            candidate = incoming.decode("latin-1")
            # SEC-09: bounded charset; anything else gets a fresh id.
            if (
                candidate
                and len(candidate) <= 128
                and all(c.isalnum() or c in "-_" for c in candidate)
            ):
                request_id = candidate
        token = request_id_var.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status") or 200)
                headers = list(message.get("headers") or [])
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                rl_headers = scope.get("state", {}).get("rate_limit_headers")
                if isinstance(rl_headers, dict):
                    for k, v in rl_headers.items():
                        headers.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))
                message["headers"] = headers
            await send(message)

        route_class = _route_class_of(str(scope.get("path") or "/"))
        INFLIGHT_REQUESTS.labels(route_class=route_class).inc()
        try:
            await self.app(scope, receive, send_with_header)
        finally:
            INFLIGHT_REQUESTS.labels(route_class=route_class).dec()
            elapsed = max(0.0, time.perf_counter() - started)
            route = scope.get("route")
            # OBS-01: a path no route matched is never a label value; that would be one
            # new series per random URL.
            path_label = getattr(route, "path", None) or "unmatched"
            HTTP_REQUESTS.labels(method=method, path=path_label, status=str(status_code)).inc()
            HTTP_LATENCY.labels(method=method, path=path_label).observe(elapsed)
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)
