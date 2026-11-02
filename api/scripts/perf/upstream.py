"""Fake upstreams for the load bench (SearXNG JSON, HTML pages, OpenAI-compatible SSE).

Latencies are deliberately fixed so differences between scenarios come from MetaSearch.
Listens on 127.0.0.1:18766 by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

BASE_HOST = "127.0.0.1"
BASE_PORT = 18766
BASE = f"http://{BASE_HOST}:{BASE_PORT}"

PARA = (
    "<p>Cổ phiếu HQC ghi nhận thanh khoản tăng trong phiên, khối ngoại mua ròng, kết quả kinh "
    "doanh quý ba cải thiện so với cùng kỳ năm trước theo báo cáo tài chính đã công bố.</p>"
)
HTML_100K = (
    "<html><head><title>Bai viet</title></head><body><article><h1>Tieu de</h1>"
    + PARA * 400
    + "</article></body></html>"
).encode()

COUNTS: dict[str, int] = {"chat": 0, "search": 0, "page": 0}


async def search(request: Request) -> JSONResponse:
    COUNTS["search"] += 1
    await asyncio.sleep(0.10)
    q = request.query_params.get("q", "q")
    salt = abs(hash(q)) % 100000
    results = [
        {
            "url": f"{BASE}/page/{salt}/{i}",
            "title": f"HQC bao cao tai chinh {i}",
            "content": "HQC thanh khoan khoi ngoai mua rong bao cao tai chinh quy ba",
            "engines": ["google", "bing"] if i % 2 else ["duckduckgo"],
            "score": float(20 - i),
        }
        for i in range(15)
    ]
    return JSONResponse({"query": q, "results": results, "answers": [], "infoboxes": []})


async def page(request: Request) -> Response:
    COUNTS["page"] += 1
    await asyncio.sleep(0.15)
    return Response(HTML_100K, media_type="text/html")


async def robots(request: Request) -> PlainTextResponse:
    return PlainTextResponse("", status_code=404)


async def counts(request: Request) -> JSONResponse:
    snapshot = dict(COUNTS)
    for key in COUNTS:
        COUNTS[key] = 0
    return JSONResponse(snapshot)


async def chat(request: Request) -> StreamingResponse:
    COUNTS["chat"] += 1
    _ = await request.json()

    async def gen():
        await asyncio.sleep(0.50)
        for i in range(120):
            chunk = {"choices": [{"delta": {"content": f"tu{i} [1] "}}]}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            await asyncio.sleep(0.015)
        usage = {"usage": {"prompt_tokens": 1000, "completion_tokens": 120}, "choices": []}
        yield f"data: {json.dumps(usage)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def create_upstream_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/search", search),
            Route("/page/{salt}/{i}", page),
            Route("/robots.txt", robots),
            Route("/__counts", counts),
            Route("/v1/chat/completions", chat, methods=["POST"]),
            Route("/healthz", healthz),
        ]
    )


app = create_upstream_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake upstream server for MetaSearch bench")
    parser.add_argument("--host", default=BASE_HOST, help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=BASE_PORT, help="Port to bind (default: 18766)")
    args = parser.parse_args()

    global BASE
    BASE = f"http://{args.host}:{args.port}"
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
