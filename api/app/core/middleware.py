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


@__import__("functools").lru_cache(maxsize=64)
def _route_class_of(path: str) -> str:
    """Bound route label: the first path segment, never the raw path.

    ``/v1/search`` -> ``/v1``, ``/v1/queries/abc`` -> ``/v1``; cardinally
    bounded because the set of top-level segments is fixed.
    """
    parts = [p for p in (path or "").split("/") if p]
    if not parts:
        return "/"
    return "/" + parts[0]


# Phase 4 P1: inbound JSON body cap (docs/23 P1 #5). Only applies to
# JSON-body routes — SSE/file/stream surfaces set their own caps.
MAX_BODY_BYTES = 1024 * 1024
_BODY_TOO_LARGE = {"error": {"code": "invalid_request", "message": "request body too large"}}


class RequestContextMiddleware:
    """Assign a request id, expose it to logs, echo it in response, and record HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Audit Phase 4 P1: reject oversized JSON bodies BEFORE buffering — a
        # 100MB body was fully read then rejected field-by-field by pydantic.
        # GET/HEAD have no body; non-JSON content types are not our surface.
        # Review F-08: the Content-Length pre-check below only sees honest clients; a
        # chunked request has no such header. body_cap also drives the streaming byte
        # counter wrapped around `receive` further down.
        body_cap: int | None = None
        if str(scope.get("method") or "").upper() in ("POST", "PUT", "PATCH"):
            headers = dict(scope.get("headers") or [])
            length = headers.get(b"content-length")
            from app.config import get_settings

            cap = int(getattr(get_settings(), "max_body_bytes", MAX_BODY_BYTES))
            body_cap = cap
            if length and length.isdigit() and int(length) > cap:
                from starlette.responses import JSONResponse

                resp = JSONResponse(_BODY_TOO_LARGE, status_code=413)
                await resp(scope, receive, send)
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

        # Streaming body cap: count the bytes actually received (works for chunked
        # transfer too). On overflow answer 413 ourselves, feed the app a disconnect so it
        # stops reading, and swallow whatever it tries to send afterwards.
        over_limit = False
        response_started = False
        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal over_limit, received_bytes
            message = await receive()
            if body_cap is None or message["type"] != "http.request":
                return message
            received_bytes += len(message.get("body") or b"")
            if received_bytes > body_cap and not over_limit:
                over_limit = True
                if not response_started:
                    from starlette.responses import JSONResponse

                    resp = JSONResponse(_BODY_TOO_LARGE, status_code=413)
                    await resp(scope, receive, send_with_header)
            if over_limit:
                return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if over_limit:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send_with_header(message)

        route_class = _route_class_of(str(scope.get("path") or "/"))
        INFLIGHT_REQUESTS.labels(route_class=route_class).inc()
        try:
            try:
                await self.app(scope, limited_receive, guarded_send)
            except Exception:
                # The app aborted because we cut its body; the 413 is already on the wire.
                if not over_limit:
                    raise
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


# docs/34 (v4.5.0): baseline security headers (OWASP Secure Headers Project).
_BASE_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # Never leak the page URL (it may carry a query) to the cited sources the UI links to.
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    # Web UI dictation (Web Speech) needs the microphone on our own origin only.
    (b"permissions-policy", b"camera=(), geolocation=(), payment=(), usb=(), microphone=(self)"),
)
# JSON/SSE/audio API responses never render as documents: lock them down fully.
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")


class SecurityHeadersMiddleware:
    """Pure-ASGI: add security headers to every HTTP response (SSE-safe —
    headers only, the body stream is untouched). Headers the app already set
    win, so a route can deliberately relax one."""

    def __init__(self, app: ASGIApp, *, hsts_max_age_s: int = 0) -> None:
        self.app = app
        self._extra: list[tuple[bytes, bytes]] = []
        if hsts_max_age_s > 0:
            self._extra.append(
                (
                    b"strict-transport-security",
                    f"max-age={int(hsts_max_age_s)}; includeSubDomains".encode(),
                )
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        is_api = path.startswith("/v1/") or path in ("/healthz", "/readyz", "/metrics")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                wanted = list(_BASE_SECURITY_HEADERS) + self._extra
                if is_api:
                    wanted.append(_API_CSP)
                for key, value in wanted:
                    if key not in present:
                        headers.append((key, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
