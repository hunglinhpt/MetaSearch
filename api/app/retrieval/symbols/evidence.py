"""Per-symbol tagging evidence (docs/35 M1).

Each tagged ticker carries HOW it was found so a consumer can apply its own
precision policy (e.g. accept only ``prefix|exchange|cashtag|phrase`` plus
``alias`` with ``alias_confidence >= 0.9``) instead of re-parsing headlines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EvidenceMethod = Literal["prefix", "exchange", "cashtag", "phrase", "bare", "alias", "caller"]
ALL_METHODS: frozenset[str] = frozenset(
    {"prefix", "exchange", "cashtag", "phrase", "bare", "alias", "caller"}
)
STRONG_METHODS: frozenset[str] = frozenset({"prefix", "exchange", "cashtag", "phrase", "caller"})


@dataclass(frozen=True, slots=True)
class SymbolEvidence:
    symbol: str
    method: EvidenceMethod
    span: str
    start: int
    field: str = "text"
    alias_confidence: float | None = None

    def with_field(self, field: str, offset: int = 0) -> SymbolEvidence:
        return SymbolEvidence(
            self.symbol, self.method, self.span, self.start - offset, field, self.alias_confidence
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "method": self.method,
            "span": self.span,
            "start": self.start,
            "field": self.field,
        }
        if self.alias_confidence is not None:
            out["alias_confidence"] = self.alias_confidence
        return out


def split_by_field(
    evidence: list[SymbolEvidence], *, title_len: int, sep_len: int = 1
) -> list[SymbolEvidence]:
    """Evidence computed on ``f"{title} {snippet}"`` -> per-field offsets."""
    out: list[SymbolEvidence] = []
    snippet_start = title_len + sep_len
    for ev in evidence:
        if ev.start < title_len:
            out.append(ev.with_field("title"))
        elif ev.start >= snippet_start:
            out.append(ev.with_field("snippet", snippet_start))
    return out
