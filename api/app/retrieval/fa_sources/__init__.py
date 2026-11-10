"""Multi-source FA aggregation — direct adapters, no third-party wrapper (docs/15).

Public surface:
- ``FASource`` / ``SourceFinancials`` / ``SourceOHLCV`` / ``FASourceError`` (contracts)
- ``FAAggregator`` / ``AggregatedFinancials`` (fan-out + cross-validate + merge)
- ``build_default_aggregator`` (SSI + TCBS + FireAnt, direct over the shared pools)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.retrieval.fa_sources.aggregator import (
    AggregatedFinancials,
    FAAggregator,
    FieldValue,
)
from app.retrieval.fa_sources.base import (
    CANONICAL_FA_FIELDS,
    MAX_RESPONSE_BYTES,
    FASource,
    FASourceError,
    SourceFinancials,
    SourceOHLCV,
)
from app.retrieval.health import EngineBreakerRegistry

if TYPE_CHECKING:
    import httpx


def build_default_aggregator(
    client: httpx.AsyncClient | None = None,
    registry: EngineBreakerRegistry | None = None,
) -> FAAggregator:
    """Process-default source set: SSI (OHLCV) + TCBS (ratios) + FireAnt (statements).

    FireAnt self-disables when ``FA_FIREANT_TOKEN`` is empty. Each source gets its
    own breaker (``fa:{source_id}``); the registry can be injected by the caller,
    otherwise a fresh one is created with the operator's
    ``engine_auto_disable_threshold`` so FA sources trip consistently with engines.
    """
    from app.config import get_settings
    from app.retrieval.fa_sources.fireant import FireAntFASource
    from app.retrieval.fa_sources.ssi import SSIFASource
    from app.retrieval.fa_sources.tcbs import TCBSFASource

    owned = registry
    if owned is None:
        owned = EngineBreakerRegistry(threshold=get_settings().engine_auto_disable_threshold)

    return FAAggregator(
        sources=[
            SSIFASource(client=client),
            TCBSFASource(client=client),
            FireAntFASource(client=client),
        ],
        registry=owned,
    )


__all__ = [
    "CANONICAL_FA_FIELDS",
    "MAX_RESPONSE_BYTES",
    "AggregatedFinancials",
    "FAAggregator",
    "FASource",
    "FASourceError",
    "FieldValue",
    "SourceFinancials",
    "SourceOHLCV",
    "build_default_aggregator",
]
