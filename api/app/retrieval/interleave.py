"""Online interleaving for retrieval changes (docs/20 P2-4).

Deterministic hash bucketing: each (query, owner) pair always lands in the same
variant, so a user never sees both variants for one query and the assignment is
reproducible from the cache key. Control = current settings; treatment =
configured override (e.g. a different RRF_WEIGHT). Feedback (👍/👎 recorded via
/v1/memory with ``payload.variant``) is aggregated per variant by
``GET /v1/ops/interleave`` — no clickbait metrics, only explicit signals.
"""

from __future__ import annotations

import hashlib
from typing import Any


def assign_variant(query: str, owner: str, *, ratio: float, salt: str = "") -> str:
    """Return "a" (control) or "b" (treatment) deterministically.

    ``ratio`` is the fraction of traffic on treatment (0..1).
    """
    if ratio <= 0.0:
        return "a"
    if ratio >= 1.0:
        return "b"
    h = hashlib.sha256(f"{salt}|{owner}|{query}".encode()).hexdigest()
    bucket = int(h[:8], 16) % 100
    return "b" if bucket < int(ratio * 100) else "a"


def variant_signature(variant: str) -> str:
    """Folded into the search cache key so variants never share an entry."""
    return f"il:{variant}"


async def interleave_stats(session: Any) -> dict[str, Any]:
    """Aggregate per-variant feedback from memory_items (kind=feedback with a
    payload.variant). Returns counts + 👍 rate per variant."""
    from sqlalchemy import select

    from app.db.models import MemoryItem

    # Bounded scan: only the most recent feedback rows (memory is capped per
    # key, but across keys the table grows — never load it whole).
    rows = (
        (
            await session.execute(
                select(MemoryItem)
                .where(
                    MemoryItem.kind == "feedback",
                    MemoryItem.payload.is_not(None),
                )
                .order_by(MemoryItem.created_at.desc())
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )
    stats: dict[str, dict[str, float]] = {}
    for row in rows:
        payload = row.payload or {}
        variant = str(payload.get("variant") or "unknown")
        bucket = stats.setdefault(variant, {"up": 0.0, "down": 0.0, "total": 0.0})
        bucket["total"] += 1
        if float(row.weight or 0) > 0:
            bucket["up"] += 1
        elif float(row.weight or 0) < 0:
            bucket["down"] += 1
    out: dict[str, Any] = {}
    for variant, b in stats.items():
        up_rate = (b["up"] / b["total"]) if b["total"] else 0.0
        out[variant] = {
            "feedback_total": int(b["total"]),
            "up": int(b["up"]),
            "down": int(b["down"]),
            "up_rate": round(up_rate, 4),
        }
    return {"variants": out, "note": "explicit 👍/👎 only; no implicit click metrics"}
