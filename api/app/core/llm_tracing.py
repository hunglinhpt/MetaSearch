"""Optional LLM tracing to Langfuse (docs/09-observability-eval.md, Phase 4).

Giants-first: a fire-and-forget POST to Langfuse's public traces API via
httpx — no SDK. Enabled only when LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST are
all set; failures are logged once and never affect requests.
"""

from __future__ import annotations

import base64
from typing import Any

from app.config import Settings
from app.core.logging import get_logger
from app.http import get_http_client

logger = get_logger(__name__)

_warned = False


def langfuse_enabled(settings: Settings) -> bool:
    return bool(
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    )


async def trace_llm_call(
    settings: Settings,
    *,
    task: str,
    model: str,
    messages: list[dict[str, str]],
    output: str,
    usage: dict[str, Any],
    latency_ms: int | None = None,
    trace_id: str | None = None,
) -> None:
    """Send one LLM-generation trace. Fire-and-forget; never raises."""
    global _warned
    if not langfuse_enabled(settings):
        return
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    payload = {
        "id": trace_id,
        "name": f"llm-{task}",
        "input": {"messages": messages},
        "output": {"completion": output[:4000]},
        "metadata": {"task": task, **usage},
        "usage": {
            "promptTokens": usage.get("input_tokens", 0),
            "completionTokens": usage.get("output_tokens", 0),
        },
        "model": model,
        **({"latencyMs": latency_ms} if latency_ms is not None else {}),
    }
    try:
        client = get_http_client("internal")
        resp = await client.post(
            f"{settings.langfuse_host.rstrip('/')}/api/public/traces",
            json=payload,
            headers={"Authorization": f"Basic {auth}"},
            timeout=5.0,
        )
        if resp.status_code >= 400:
            logger.info("langfuse_trace_rejected", status=resp.status_code)
    except Exception as exc:
        if not _warned:
            logger.warning("langfuse_unavailable", error=type(exc).__name__)
            _warned = True
