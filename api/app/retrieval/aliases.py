"""Stage B — canonical bilingual company-alias → ticker resolver (docs/30).

Three-stage resolver for wire entity tagging; this module implements Stage B:
a canonical company/brand alias dictionary built from two sources:

1. **Auto-generated from `vn_tickers.json`** — for every listed ticker the
   distinctive remainder of `short_name` / `company_name` (generic corporate
   prefixes/suffixes stripped) becomes an alias IFF the remainder is
   unambiguous (maps to exactly one symbol globally, ≥ 2 distinctive tokens
   or one ≥ 6-char token, never a generic word).
2. **Curated brand aliases** — brand names that never appear verbatim in the
   registry (`Vinamilk`, `Vietcombank`, `VinFast`, …) plus disambiguating
   overrides for multi-candidate families (`Thép Hòa Phát` / `Tập đoàn Hòa
   Phát` → HPG; bare `Hòa Phát` stays ambiguous across HTP/HPA/HPG).

Matching is a single compiled word-boundary regex alternation (O(text) per
item) over case-folded aliases; a match yields the canonical ticker which is
then filtered through the caller's known-ticker set. Ambiguous multi-candidate
remainders are deliberately never indexed (anti-collision by construction).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

_TICKERS_PATH = Path(__file__).resolve().parent / "data" / "vn_tickers.json"

# Generic corporate words stripped before an alias becomes distinctive.
_GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "công",
        "ty",
        "cổ",
        "phần",
        "ctcp",
        "cp",
        "tổng",
        "tập",
        "đoàn",
        "và",
        "việt",
        "nam",
        "vietnam",
        "vn",
        "của",
        "kinh",
        "doanh",
        "dịch",
        "vụ",
        "dịch vụ",
        "thương mại",
        "đầu",
        "tư",
        "đầu tư",
        "chứng",
        "khoán",
        "ngân",
        "hàng",
        "số",
        "một",
        "vietnamese",
        "holding",
        "company",
        "joint",
        "stock",
        "corporation",
        "group",
        "the",
        "and",
        "of",
    }
)

# Curated brand aliases (docs/29 ISSUE: bilingual brand names missing from the
# registry) — brand/subsidiary → canonical ticker. Bare-word entries that
# collide with common Vietnamese/English words live here deliberately with
# the multi-token disambiguating form instead (Stage C keeps bare-word rules).
_CURATED_ALIASES: tuple[tuple[str, str], ...] = (
    ("Vinamilk", "VNM"),
    ("Sữa Việt Nam", "VNM"),
    ("Thép Hòa Phát", "HPG"),
    ("Tập đoàn Hòa Phát", "HPG"),
    ("FPT Software", "FPT"),
    ("FPT Telecom", "FPT"),
    ("FPT Retail", "FRT"),
    ("Nhà thuốc Long Châu", "FRT"),
    ("Vietcombank", "VCB"),
    ("BIDV", "BID"),
    ("Techcombank", "TCB"),
    ("VPBank", "VPB"),
    ("VinFast", "VFS"),
    ("Vingroup", "VIC"),
    ("Vinhomes", "VHM"),
    ("Masan", "MSN"),
    ("SABECO", "SAB"),
    ("Sabeco", "SAB"),
    ("Bia Sài Gòn", "SAB"),
    ("Vietjet", "VJC"),
    ("Vietnam Airlines", "HVN"),
    ("Thaco", "THG"),
    ("Trường Hải", "THG"),
    ("Nosco", "NOS"),
    ("Thiên Long", "TLG"),
    ("Petrovietnam", "PVS"),
    ("Petro Viet Nam", "PVS"),
    ("Hoa Sen", "HSG"),
    ("Tôn Hoa Sen", "HSG"),
    ("Đầu tư Địa ốc Hoàng Quân", "HQC"),
    ("Địa ốc Hoàng Quân", "HQC"),
    ("Hoàng Quân Địa ốc", "HQC"),
    ("Hoàng Quân", "HQC"),  # unique to HQC in the registry (verified)
    ("Tư vấn Thương mại Dịch vụ Địa ốc Hoàng Quân", "HQC"),
)


def _strip_generic(tokens: list[str]) -> list[str]:
    """Drop generic corporate words from both ends and collapse whitespace."""
    while tokens and tokens[0].lower() in _GENERIC_WORDS:
        tokens = tokens[1:]
    while tokens and tokens[-1].lower() in _GENERIC_WORDS:
        tokens = tokens[:-1]
    return tokens


def _distinctive(tokens: list[str]) -> bool:
    """A remainder is distinctive enough for exact matching."""
    if len(tokens) >= 2:
        return all(len(t) >= 3 for t in tokens)
    return len(tokens) == 1 and len(tokens[0]) >= 6


def _build_index() -> tuple[dict[str, str], re.Pattern[str]]:
    """(alias → symbol map, compiled case-insensitive matcher). Build once."""
    alias_to_symbol: dict[str, str] = {}

    def _add(alias: str, symbol: str) -> None:
        folded = alias.strip().lower()
        if len(folded) < 5 or not folded:
            return  # never index short/bare-word aliases (Stage C territory)
        existing = alias_to_symbol.get(folded)
        if existing is None:
            alias_to_symbol[folded] = symbol
        elif existing != symbol:
            # ambiguous remainder (e.g. bare "Hòa Phát" across HTP/HPA/HPG)
            del alias_to_symbol[folded]
            alias_to_symbol[folded] = "\0ambiguous\0"

    try:
        data = json.loads(_TICKERS_PATH.read_text(encoding="utf-8"))
        tickers = data.get("tickers", {}) if isinstance(data, dict) else {}
        for symbol, meta in tickers.items():
            if not isinstance(meta, dict):
                continue
            for raw in (meta.get("short_name"), meta.get("company_name")):
                if not raw:
                    continue
                tokens = _strip_generic(str(raw).split())
                if _distinctive(tokens):
                    _add(" ".join(tokens), symbol)
    except Exception as exc:  # fail-open: wire tagging without the registry
        logger.warning("alias_index_registry_load_failed", error=type(exc).__name__)

    for alias, symbol in _CURATED_ALIASES:
        # curated wins over auto (ambiguity resolved by the explicit entry)
        alias_to_symbol[alias.strip().lower()] = symbol

    # purge ambiguity placeholders
    aliases = {a: s for a, s in alias_to_symbol.items() if s != "\0ambiguous\0"}
    if aliases:
        alternation = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        matcher = re.compile(rf"(?<![\wà-ỹ]){alternation}(?![\wà-ỹ])", re.IGNORECASE | re.UNICODE)
    else:
        matcher = re.compile(r"\Z\A")  # never matches
    return aliases, matcher


@lru_cache(maxsize=1)
def _alias_index() -> tuple[dict[str, str], re.Pattern[str]]:
    aliases, matcher = _build_index()
    return aliases, matcher


def resolve_alias_tickers(text: str) -> list[str]:
    """Stage B: return canonical tickers for corporate/brand aliases found in
    ``text`` (deduped, first-occurrence order). No context heuristics — the
    alias forms are themselves high-precision (full remainders / curated
    brands); bare collision-prone words are never indexed."""
    if not text or not text.strip():
        return []
    aliases, matcher = _alias_index()
    out: list[str] = []
    seen: set[str] = set()
    for match in matcher.finditer(text.lower()):
        symbol = aliases.get(match.group(0).lower())
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def reset_alias_index() -> None:
    """Test hook: force a rebuild after registry changes."""
    _alias_index.cache_clear()
