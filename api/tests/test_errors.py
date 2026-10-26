from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import ErrorCode, MetaSearchError, register_exception_handlers
from app.core.middleware import RequestContextMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise MetaSearchError(ErrorCode.NO_RESULTS, "nothing found", details={"k": 1})

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret internals")

    @app.get("/validated")
    async def validated(k: int) -> dict[str, int]:
        return {"k": k}

    return app


def test_meta_search_error_envelope() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "no_results"
    assert error["message"] == "nothing found"
    assert error["details"] == {"k": 1}
    assert error["request_id"]


def test_validation_error_is_mapped_to_invalid_request() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/validated", params={"k": "not-an-int"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_unhandled_error_does_not_leak_internals() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/crash")
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal"
    assert "secret internals" not in error["message"]


def test_unknown_route_uses_error_envelope() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_request"
