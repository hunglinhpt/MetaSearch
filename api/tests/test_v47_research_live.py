"""docs/36 R1b: live research eval scorer + golden shape; E1 strict retrieval metric."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "eval_research_live", ROOT / "scripts" / "eval_research_live.py"
)
assert _spec and _spec.loader
erl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(erl)


def test_golden_shape():
    data = json.loads((ROOT / "evals" / "research_live_golden.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 20 and len({c["id"] for c in cases}) == len(cases)
    assert {c["lang"] for c in cases} == {"vi", "en"}
    for c in cases:
        assert c["expected_domains"] and c["key_terms"] and 10 <= len(c["query"]) <= 500


def test_term_coverage_keeps_accents():
    report = "Thuế thu nhập cá nhân tính theo biểu lũy tiến; MVCC và VACUUM."
    assert erl.term_coverage(report, ["thuế thu nhập cá nhân", "lũy tiến"]) == 1.0
    assert erl.term_coverage("thue thu nhap ca nhan", ["thuế thu nhập cá nhân"]) == 0.0
    assert erl.term_coverage(report, ["mvcc", "xmin"]) == 0.5


def test_domain_hit_matches_subdomains_only():
    cites = [{"url": "https://www.postgresql.org/docs/current/mvcc.html"}]
    assert erl.domain_hit(cites, ["postgresql.org"]) == 1.0
    assert erl.domain_hit([{"url": "https://notpostgresql.org/x"}], ["postgresql.org"]) == 0.0


def test_score_case_and_summary():
    case = {"id": "x", "expected_domains": ["redis.io"], "key_terms": ["stream"]}
    done = {
        "status": "done",
        "report_md": "Redis Stream [1].",
        "citations": [{"n": 1, "url": "https://redis.io/docs"}],
        "verification": {
            "citation_count": 2,
            "unsupported_markers": [2],
            "citation_coverage": 1.0,
            "numeric_fidelity": 1.0,
        },
    }
    row = erl.score_case(case, done, 12.0)
    assert row["completed"] == 1.0 and row["domain_hit"] == 1.0 and row["unsupported_ratio"] == 0.5
    failed = erl.score_case(case, {"status": "failed", "error": "LLM"}, 3.0)
    assert failed["completed"] == 0.0 and failed["unsupported_ratio"] is None
    s = erl.summarize([row, failed])
    assert s["completed_rate"] == 0.5 and s["unsupported_ratio"] == 0.5


def test_strict_hit_requires_topic():
    from app.eval_metrics import strict_hit_at_k, topical_match

    q = "cách nấu phở bò ngon đúng vị Hà Nội"
    off = [("https://vnexpress.net/chanh", "Vì sao chanh ngón tay có nơi giá 2 triệu")]
    on = [("https://vnexpress.net/pho", "Cách nấu phở bò Hà Nội chuẩn vị")]
    assert strict_hit_at_k(off, ["vnexpress.net"], q) == 0.0
    assert strict_hit_at_k(on, ["vnexpress.net"], q) == 1.0
    assert topical_match("gia vang hom nay", "Giá vàng hôm nay tăng")  # unaccented folds
    assert not topical_match("", "anything")
