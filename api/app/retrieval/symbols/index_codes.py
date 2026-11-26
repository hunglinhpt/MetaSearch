"""Market index codes (docs/35 M1).

Index codes are not equities: they never enter ``symbols`` through bare tokens
or aliases. A news item about an index reports it in ``indices`` instead. A
listed company that happens to share an index code (``VNI`` on UPCOM) is still
taggable with EXPLICIT evidence only (``UPCOM: VNI``, "cổ phiếu VNI", ``VNI:``).
"""

from __future__ import annotations

import re

INDEX_CODES: frozenset[str] = frozenset(
    {"VNI", "VNINDEX", "VN30", "VN100", "HNX", "HNXINDEX", "HNX30", "UPCOM", "UPCOMINDEX"}
)

# (canonical index, pattern) — written forms seen in Vietnamese financial copy.
_INDEX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("VNINDEX", re.compile(r"(?i)\bVN[-\s]?Index\b|\bVNINDEX\b|(?<![\w])VNI(?![\w])")),
    ("VN30", re.compile(r"(?i)\bVN30(?:[-\s]?Index)?\b")),
    ("VN100", re.compile(r"(?i)\bVN100(?:[-\s]?Index)?\b")),
    ("HNXINDEX", re.compile(r"(?i)\bHNX[-\s]?Index\b|\bHNXINDEX\b")),
    ("HNX30", re.compile(r"(?i)\bHNX30(?:[-\s]?Index)?\b")),
    ("UPCOMINDEX", re.compile(r"(?i)\bUPCoM[-\s]?Index\b|\bUPCOMINDEX\b")),
)


def is_index_code(sym: str) -> bool:
    return (sym or "").upper() in INDEX_CODES


def extract_indices(text: str) -> list[str]:
    """Canonical index names mentioned in ``text`` (first-occurrence order)."""
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for name, pat in _INDEX_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append((m.start(), name))
    return [name for _pos, name in sorted(hits)]
