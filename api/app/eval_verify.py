"""Scoring for the claim-verification gold set (docs/36 T1).

Safety first: a verifier that calls a false claim "supported" is worse than
one that says "insufficient evidence". The primary metric is therefore
``false_support_rate`` (false claims judged ``supported``; target <= 0.05),
then ``supported_precision`` and ``true_recall`` (true claims judged
``supported``). ``refuted`` on a false claim is a bonus (``false_refute_rate``);
``refuted`` on a TRUE claim is the worst error and counted separately.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

TARGET_FALSE_SUPPORT_RATE = 0.05


def score(gold: list[dict[str, Any]], verdicts: dict[str, str | None]) -> dict[str, Any]:
    """``verdicts[id]`` = API verdict, or None when the call failed."""
    n_true = n_false = 0
    true_supported = true_refuted = 0
    false_supported = false_refuted = 0
    errors = 0
    by_kind: dict[str, Counter[str]] = {}
    for item in gold:
        v = verdicts.get(item["id"])
        kind = str(item.get("kind") or "other")
        by_kind.setdefault(kind, Counter())[v or "error"] += 1
        if v is None:
            errors += 1
            continue
        if item["truth"]:
            n_true += 1
            true_supported += v == "supported"
            true_refuted += v == "refuted"
        else:
            n_false += 1
            false_supported += v == "supported"
            false_refuted += v == "refuted"
    predicted_supported = true_supported + false_supported

    def _ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    fsr = _ratio(false_supported, n_false)
    return {
        "n_claims": len(gold),
        "n_scored": n_true + n_false,
        "errors": errors,
        "false_support_rate": fsr,
        "supported_precision": _ratio(true_supported, predicted_supported),
        "true_recall": _ratio(true_supported, n_true),
        "true_refuted_rate": _ratio(true_refuted, n_true),
        "false_refute_rate": _ratio(false_refuted, n_false),
        "by_kind": {k: dict(c) for k, c in sorted(by_kind.items())},
        "passed": fsr is not None and fsr <= TARGET_FALSE_SUPPORT_RATE and errors == 0,
    }
