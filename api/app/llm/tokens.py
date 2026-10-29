"""Token counting for the answer context budget (docs/03-search-pipeline.md).

``CONTEXT_TOKENIZER=words`` (default) counts words — no model, no network,
deterministic. ``CONTEXT_TOKENIZER=tiktoken`` uses ``cl100k_base`` when the
optional dependency is installed (``pip install '.[tokens]'``); the BPE file
downloads once from the OpenAI CDN and is cached on disk. Any failure in the
tiktoken path (package missing, offline) falls back to words with a one-time
warning so the pipeline never breaks.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_encoding = None
_warned = False


def _words_len(text: str) -> int:
    return len(text.split())


def _tiktoken_len(text: str) -> int:
    global _encoding
    if _encoding is None:
        import tiktoken

        _encoding = tiktoken.get_encoding("cl100k_base")
    # disallowed_special=() so scraped web content never raises on tokens
    # that happen to look like special markers.
    return len(_encoding.encode(text, disallowed_special=()))


def make_counter(settings: Settings) -> Callable[[str], int]:
    """Return a ``text -> tokens`` counter honoring ``CONTEXT_TOKENIZER``."""
    if settings.context_tokenizer != "tiktoken":
        return _words_len

    global _warned
    try:
        probe = _tiktoken_len("warm up")
        logger.info("token_counter_ready", backend="tiktoken", probe=probe)
    except Exception as exc:
        if not _warned:
            logger.warning(
                "token_counter_fallback_words",
                reason=str(exc)[:120],
                hint="pip install '.[tokens]' (and network for the BPE file)",
            )
            _warned = True
        return _words_len
    return _tiktoken_len
