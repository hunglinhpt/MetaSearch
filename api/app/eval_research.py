"""Offline evaluator for Deep Research reports using FACT metrics:
- citation_coverage: share of substantive sentences carrying inline [n] citations
- citation_precision: share of [n] markers resolving to included sources
- claim_support: share of cited sentences supported by lexical entailment
- numeric_fidelity: share of numeric figures grounded in cited chunks
- outline_coverage: fraction of planned outline headings reflected in report
- conflict_surfaced: whether planned source conflicts are explicitly discussed
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.eval_metrics import (
    citation_precision_rate,
    claim_support_rate,
    numeric_fidelity_rate,
)
from app.llm.citations import split_sentences
from app.retrieval.scoring import fold_diacritics, tokenize

DEFAULT_RESEARCH_GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "research_golden.json"
_CITE_RE = re.compile(r"\[\d{1,3}\]")


def _body_without_references(report_md: str) -> str:
    """Exclude trailing References/Bibliography section from claim-level prose checks."""
    m = re.search(
        r"^##\s+(?:References|Tài\s+liệu\s+tham\s+khảo)\b",
        report_md or "",
        re.IGNORECASE | re.MULTILINE,
    )
    return (report_md or "")[: m.start()].strip() if m else (report_md or "")


def evaluate_research_report(
    report_md: str,
    chunks: dict[int, str],
    *,
    outline: list[str] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """Compute FACT & structural metrics for one synthesized research report."""
    prose_md = _body_without_references(report_md)
    substantive = [
        s
        for s in split_sentences(prose_md)
        if len(s.strip()) > 20 and not s.lstrip().startswith("#")
    ]
    cited_cnt = sum(1 for s in substantive if _CITE_RE.search(s))
    cit_cov = (cited_cnt / len(substantive)) if substantive else 0.0
    cit_prec = citation_precision_rate(prose_md, set(chunks))
    c_sup = claim_support_rate(prose_md, chunks) if prose_md else 0.0
    num_fid = numeric_fidelity_rate(prose_md, chunks)

    outline_cov = 1.0
    if outline:
        body_folded = fold_diacritics((report_md or "").lower())
        hits = 0
        for heading in outline:
            terms = [t for t in tokenize(heading, fold=True) if len(t) > 2]
            if not terms or any(t in body_folded for t in terms):
                hits += 1
        outline_cov = hits / len(outline)

    conflict_cov = 1.0
    if conflicts:
        body_folded = fold_diacritics((report_md or "").lower())
        surfaced = 0
        for c in conflicts:
            topic_terms = [t for t in tokenize(str(c.get("topic") or ""), fold=True) if len(t) > 2]
            if not topic_terms or any(t in body_folded for t in topic_terms):
                surfaced += 1
        conflict_cov = surfaced / len(conflicts)

    fact_score = (
        0.25 * cit_cov
        + 0.20 * cit_prec
        + 0.25 * c_sup
        + 0.20 * num_fid
        + 0.10 * ((outline_cov + conflict_cov) / 2.0)
    )
    return {
        "citation_coverage": round(cit_cov, 4),
        "citation_precision": round(cit_prec, 4),
        "claim_support": round(c_sup, 4),
        "numeric_fidelity": round(num_fid, 4),
        "outline_coverage": round(outline_cov, 4),
        "conflict_surfaced": round(conflict_cov, 4),
        "fact_score": round(fact_score, 4),
    }


def evaluate_research_golden(path: Path = DEFAULT_RESEARCH_GOLDEN) -> dict[str, Any]:
    """Run offline FACT evaluation over ``research_golden.json``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    thresholds = payload.get("thresholds") or {}
    records: list[dict[str, Any]] = []
    for case in cases:
        chunks = {int(k): str(v) for k, v in (case.get("chunks") or {}).items()}
        metrics = evaluate_research_report(
            str(case.get("report_md") or ""),
            chunks,
            outline=case.get("outline"),
            conflicts=case.get("conflicts"),
        )
        records.append({"id": case["id"], **metrics})
    keys = (
        "citation_coverage",
        "citation_precision",
        "claim_support",
        "numeric_fidelity",
        "outline_coverage",
        "conflict_surfaced",
        "fact_score",
    )
    summary = {
        k: round(sum(r[k] for r in records) / len(records), 4) if records else 0.0 for k in keys
    }
    summary["n_cases"] = len(records)
    passed = all(summary.get(k, 0.0) >= float(v) for k, v in thresholds.items())
    return {
        "summary": summary,
        "thresholds": thresholds,
        "passed": passed,
        "records": records,
    }
