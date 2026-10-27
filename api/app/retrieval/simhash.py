"""64-bit SimHash for near-duplicate detection (docs/03-search-pipeline.md §3).

Two results whose title+snippet hashes sit within ``HAMMING_THRESHOLD`` bits are
considered the same content (mirrors, syndicated copies) and get merged.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import Counter

from app.retrieval.scoring import tokenize

HAMMING_THRESHOLD = 3
_BITS = 64

_hash_mask = (1 << _BITS) - 1


def _token_hash(token: str) -> int:
    # Stable across processes (unlike hash()); md5 truncated is fine here —
    # this is a similarity key, not a security primitive.
    digest = hashlib.md5(token.encode("utf-8")).digest()  # nosec B324
    return int.from_bytes(digest[:8], "big")


def simhash64(text: str) -> int:
    """Weighted SimHash over normalized tokens; empty text hashes to 0."""
    tokens = tokenize(unicodedata.normalize("NFC", text))
    if not tokens:
        return 0
    counts = Counter(tokens)
    bits = [0] * _BITS
    for token, weight in counts.items():
        h = _token_hash(token)
        for i in range(_BITS):
            if (h >> i) & 1:
                bits[i] += weight
            else:
                bits[i] -= weight
    value = 0
    for i, bit in enumerate(bits):
        if bit > 0:
            value |= 1 << i
    return value & _hash_mask


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a: int, b: int, *, threshold: int = HAMMING_THRESHOLD) -> bool:
    if a == 0 or b == 0:  # no signal — never call empty content a duplicate
        return False
    return hamming_distance(a, b) <= threshold
