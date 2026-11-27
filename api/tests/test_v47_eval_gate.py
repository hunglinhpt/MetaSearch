"""docs/36 S2: eval regression gate + committed baselines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval_gate import compare, load_report

BASELINES = Path(__file__).resolve().parent.parent / "evals" / "baselines"


def _r(**summary) -> dict:
    return {"summary": {"n_queries": 29, **summary}}


def test_drop_beyond_margin_fails_and_is_reported() -> None:
    res = compare(_r(hit_at_5=0.38, mrr=0.26), _r(hit_at_5=0.30, mrr=0.25), max_drop=0.05)
    assert res.passed is False
    assert res.regressions["hit_at_5"]["delta"] == -0.08
    assert "mrr" not in res.regressions


def test_small_noise_and_improvements_pass() -> None:
    res = compare(_r(hit_at_5=0.38, mrr=0.26), _r(hit_at_5=0.36, mrr=0.40))
    assert res.passed is True
    assert res.improvements["mrr"]["delta"] == 0.14


def test_missing_metrics_are_skipped_but_some_must_compare() -> None:
    res = compare(_r(hit_at_5=0.4, ndcg_at_10=0.2), _r(hit_at_5=0.4))
    assert res.compared == ["hit_at_5"] and "ndcg_at_10" in res.skipped
    with pytest.raises(ValueError):
        compare(_r(foo=1), _r(bar=2))


def test_different_golden_sets_are_refused() -> None:
    with pytest.raises(ValueError, match="golden set size"):
        compare(
            {"summary": {"n_queries": 29, "hit_at_5": 0.4}},
            {"summary": {"n_queries": 25, "hit_at_5": 0.4}},
        )
    with pytest.raises(ValueError, match="summary"):
        compare({}, _r(hit_at_5=0.1))


@pytest.mark.parametrize("name", ["eval_v2_golden_v2.lite.json", "eval_v2_golden_v3_vi.lite.json"])
def test_committed_baselines_are_well_formed(name: str) -> None:
    data = load_report(BASELINES / name)
    meta = data["baseline_meta"]
    assert meta["version"] and meta["measured_on"] and meta["golden"].startswith("evals/")
    golden = json.loads((BASELINES.parent.parent / meta["golden"]).read_text(encoding="utf-8"))
    queries = golden["queries"] if isinstance(golden, dict) else golden
    assert data["summary"]["n_queries"] == len(queries)
    assert compare(data, data).passed  # self-comparison is a no-op


def test_cli_exit_codes(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(BASELINES.parent.parent))
    from scripts.eval_gate import main

    b = tmp_path / "b.json"
    c = tmp_path / "c.json"
    b.write_text(json.dumps(_r(hit_at_5=0.5)), encoding="utf-8")
    c.write_text(json.dumps(_r(hit_at_5=0.3)), encoding="utf-8")
    assert main(["--baseline", str(b), "--report", str(c)]) == 1
    assert main(["--baseline", str(b), "--report", str(b)]) == 0
    assert main(["--baseline", str(b), "--report", str(tmp_path / "missing.json")]) == 2
