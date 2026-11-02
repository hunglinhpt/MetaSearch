"""Canonical error model shared by every endpoint (see docs/06-api-spec.md)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    NO_RESULTS = "no_results"
    INTERNAL = "internal"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UPSTREAM_TIMEOUT: 502,
    ErrorCode.NO_RESULTS: 503,
    ErrorCode.INTERNAL: 500,
}


class MetaSearchError(Exception):
    """Domain error carrying a stable machine-readable code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code or HTTP_STATUS[code]
        self.headers = headers or {}

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so every failure uses the same JSON envelope."""

    @app.exception_handler(MetaSearchError)
    async def _handle_metasearch_error(request: Request, exc: MetaSearchError) -> JSONResponse:
        logger.warning(
            "request_failed",
            code=exc.code.value,
            message=exc.message,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(_request_id(request)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        err = MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "Request validation failed",
            details={"errors": exc.errors()},
        )
        return JSONResponse(
            status_code=err.status_code, content=err.to_payload(_request_id(request))
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.UNAUTHORIZED if exc.status_code == 401 else ErrorCode.INVALID_REQUEST
        if exc.status_code >= 500:
            code = ErrorCode.INTERNAL
        err = MetaSearchError(code, str(exc.detail), status_code=exc.status_code)
        return JSONResponse(
            status_code=err.status_code, content=err.to_payload(_request_id(request))
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path)
        err = MetaSearchError(ErrorCode.INTERNAL, "Internal server error")
        return JSONResponse(
            status_code=err.status_code, content=err.to_payload(_request_id(request))
        )
