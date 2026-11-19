"""Offline evaluator for the v4.0 Bilingual Equity Sentiment Engine (macro-F1,
collision rejection, SEntFiN attribution, and integrity audit checks).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.retrieval.sentiment import (
    RawSentimentHit,
    analyze_omni_channel_sentiment,
    resolve_equity_entity,
)

DEFAULT_SENTIMENT_GOLDEN = (
    Path(__file__).resolve().parent.parent / "evals" / "sentiment_golden.json"
)
_CLASSES = ("BULLISH", "NEUTRAL", "BEARISH")


def collapse_sentiment_label(raw_label: str) -> str:
    upper = (raw_label or "NEUTRAL").upper()
    if upper in ("STRONG_BULLISH", "BULLISH"):
        return "BULLISH"
    if upper in ("STRONG_BEARISH", "BEARISH"):
        return "BEARISH"
    return "NEUTRAL"


def evaluate_sentiment_golden(
    path: Path = DEFAULT_SENTIMENT_GOLDEN,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run offline evaluation over ``sentiment_golden.json`` and compute metrics."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    ref_now = now or datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

    records: list[dict[str, Any]] = []
    collision_total = 0
    collision_dropped = 0
    sentfin_total = 0
    sentfin_correct = 0

    for case in cases:
        ticker = str(case["ticker"])
        entity = resolve_equity_entity(ticker, now=ref_now)
        raw_lang = str(case.get("lang") or "VI").upper()
        lang_val: Literal["VI", "EN"] = "EN" if raw_lang == "EN" else "VI"
        hits = [
            RawSentimentHit(
                url=str(h["url"]),
                title=str(h.get("title") or ""),
                snippet=str(h.get("snippet") or ""),
                published_at=ref_now,
                lang=lang_val,
                source_url=h.get("source_url"),
            )
            for h in (case.get("hits") or [])
        ]
        out = analyze_omni_channel_sentiment(entity, hits, now=ref_now)
        pred = collapse_sentiment_label(out["sentiment_summary"]["label"])
        expected = str(case["expected_label"]).upper()
        case_type = str(case.get("case_type") or "standard")

        audit_ok = True
        if case.get("expect_collision_dropped"):
            collision_total += 1
            dropped = out.get("hits_after_collision_filter", -1) == 0
            if dropped:
                collision_dropped += 1
            audit_ok = audit_ok and dropped
        if case.get("expect_seeding_flagged"):
            audit_ok = audit_ok and bool(
                out["social_integrity_audit"]["manipulation_or_seeding_detected"]
            )
        if case.get("expect_unverified_rumor") is not None:
            rumor_cnt = int(out["social_integrity_audit"]["unverified_rumor_count"])
            exp_rumor = int(case["expect_unverified_rumor"])
            audit_ok = audit_ok and (
                (rumor_cnt == 0) if exp_rumor == 0 else (rumor_cnt >= exp_rumor)
            )
        if case_type == "sentfin":
            sentfin_total += 1
            if pred == expected:
                sentfin_correct += 1

        records.append(
            {
                "id": case["id"],
                "ticker": ticker,
                "case_type": case_type,
                "expected": expected,
                "predicted": pred,
                "raw_label": out["sentiment_summary"]["label"],
                "score": out["sentiment_summary"]["composite_score"],
                "correct": pred == expected and audit_ok,
                "audit_ok": audit_ok,
            }
        )

    per_class: dict[str, dict[str, float]] = {}
    f1_scores: list[float] = []
    for cls in _CLASSES:
        tp = sum(1 for r in records if r["predicted"] == cls and r["expected"] == cls)
        fp = sum(1 for r in records if r["predicted"] == cls and r["expected"] != cls)
        fn = sum(1 for r in records if r["predicted"] != cls and r["expected"] == cls)
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
        per_class[cls] = {
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "support": tp + fn,
        }
        f1_scores.append(f1)

    accuracy = sum(1 for r in records if r["correct"]) / len(records) if records else 0.0
    macro_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0
    threshold = float(payload.get("threshold_macro_f1", 0.85))
    return {
        "n_cases": len(records),
        "accuracy": round(accuracy, 4),
        "macro_f1": round(macro_f1, 4),
        "threshold_macro_f1": threshold,
        "passed": macro_f1 >= threshold,
        "collision_rejection_rate": (
            round(collision_dropped / collision_total, 4) if collision_total else 1.0
        ),
        "sentfin_attribution_accuracy": (
            round(sentfin_correct / sentfin_total, 4) if sentfin_total else 1.0
        ),
        "per_class": per_class,
        "errors": [r for r in records if not r["correct"]],
    }
