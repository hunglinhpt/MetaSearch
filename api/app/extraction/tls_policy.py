"""TLS verification policy for the scrape fallback chain (docs/35 M4, v4.6.0).

A direct fetch that fails because the origin's certificate does not verify
(self-signed, expired, wrong host, unknown CA) MUST NOT be silently retried
through crawl4ai / Jina: those services would fetch the page on our behalf and
hand back content the verified connection had just refused — with no sign of
it to the caller. Plain network drops (resets, ``UNEXPECTED_EOF`` — common with
anti-bot middleboxes) are a different failure and keep escalating as before.
"""

from __future__ import annotations

import ssl

# Stable machine-readable failure reason (PageFailure.error / ScrapeItem.error).
TLS_VERIFY_FAILED = "tls_verify_failed"
_VERIFY_MARKERS = ("certificate_verify_failed", "certificate verify failed")
_MAX_CHAIN = 16


class TLSVerificationFailed(Exception):
    """The origin's TLS certificate did not verify; escalation was refused."""

    def __init__(self, url: str) -> None:
        super().__init__(f"TLS certificate verification failed for {url}")
        self.url = url


def is_tls_verification_error(exc: BaseException | None) -> bool:
    """True when ``exc`` (or anything in its cause/context chain) is a
    certificate-verification failure. ``SSLEOFError`` / ``UNEXPECTED_EOF`` are
    network drops, never certificate errors."""
    seen: set[int] = set()
    cur = exc
    depth = 0
    while cur is not None and id(cur) not in seen and depth < _MAX_CHAIN:
        seen.add(id(cur))
        depth += 1
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if isinstance(cur, ssl.SSLEOFError):
            return False
        text = str(cur).lower()
        if "unexpected_eof" in text or "eof occurred" in text:
            return False
        if any(m in text for m in _VERIFY_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False
