"""Quality regression gate against committed eval baselines (docs/36 S2).

A baseline is an ``eval_v2`` report committed under ``evals/baselines/`` (with
the version, profile and date it was measured on). The gate compares a fresh
report's summary to it and fails when any gated metric DROPS by more than the
allowed margin. Improvements never fail; they are reported so the baseline can
be refreshed deliberately (a reviewed commit), never silently.

Absolute thresholds (``hit_at_5 >= 0.6`` …) stay in ``eval_v2`` as the target;
this gate answers the different question "did this change make things worse
than what we shipped?" — the only question a nightly run against volatile live
engines can answer fairly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_METRICS: tuple[str, ...] = ("hit_at_5", "hit_at_10", "mrr", "ndcg_at_10", "strict_hit_at_5")
DEFAULT_MAX_DROP = 0.05


@dataclass(slots=True)
class GateResult:
    passed: bool
    regressions: dict[str, dict[str, float]] = field(default_factory=dict)
    improvements: dict[str, dict[str, float]] = field(default_factory=dict)
    compared: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "compared": self.compared,
            "skipped": self.skipped,
        }


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    s = report.get("summary")
    if not isinstance(s, dict):
        raise ValueError("report has no 'summary' object")
    return s


def compare(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    metrics: tuple[str, ...] = DEFAULT_METRICS,
    max_drop: float = DEFAULT_MAX_DROP,
) -> GateResult:
    """Pure comparison of two eval_v2 reports."""
    b, c = _summary(baseline), _summary(current)
    result = GateResult(passed=True)
    if b.get("n_queries") and c.get("n_queries") and b["n_queries"] != c["n_queries"]:
        # Different golden sets are not comparable: refuse instead of guessing.
        raise ValueError(
            f"golden set size differs (baseline {b['n_queries']}, current {c['n_queries']})"
        )
    for m in metrics:
        bv, cv = b.get(m), c.get(m)
        if not isinstance(bv, int | float) or not isinstance(cv, int | float):
            result.skipped.append(m)
            continue
        result.compared.append(m)
        delta = round(float(cv) - float(bv), 4)
        row = {"baseline": float(bv), "current": float(cv), "delta": delta}
        if delta < -max_drop:
            result.regressions[m] = row
            result.passed = False
        elif delta > max_drop:
            result.improvements[m] = row
    if not result.compared:
        raise ValueError("no comparable metrics between baseline and current report")
    return result


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data
