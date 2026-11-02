"""Compare two benchmark result JSON files and check for regressions.

Usage:
  python api/scripts/perf/compare.py baseline.json current.json --max-regress 10

Exits with code 1 if any scenario regresses beyond the threshold percentage.
"""

from __future__ import annotations

import argparse
import json
import sys


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare MetaSearch benchmark JSON results")
    parser.add_argument("baseline", help="Path to baseline JSON file")
    parser.add_argument("current", help="Path to current JSON file")
    parser.add_argument(
        "--max-regress",
        type=float,
        default=10.0,
        help="Maximum allowed percentage regression before failing (default: 10.0)",
    )
    args = parser.parse_args()

    base_data = load_json(args.baseline)
    curr_data = load_json(args.current)

    base_scenarios = {
        (s["label"], s.get("concurrency", 0)): s for s in base_data.get("scenarios", [])
    }
    curr_scenarios = {
        (s["label"], s.get("concurrency", 0)): s for s in curr_data.get("scenarios", [])
    }

    print("=" * 88)
    print(
        f"Benchmark Comparison: {args.baseline} (baseline) vs {args.current} (current) "
        f"[Max allowed regression: {args.max_regress}%]"
    )
    print("=" * 88)

    regressions: list[str] = []

    header = (
        f"{'Scenario':<30} {'c':<3} {'Metric':<8} {'Baseline':>10} "
        f"{'Current':>10} {'Change':>10} {'Status':<10}"
    )
    print(header)
    print("-" * len(header))

    threshold_fraction = args.max_regress / 100.0

    for key, base_s in base_scenarios.items():
        label, conc = key
        if key not in curr_scenarios:
            b_val = base_s.get("p95_ms", 0)
            print(
                f"{label:<30} {conc:<3} {'MISSING':<8} {b_val:>10.1f} "
                f"{'N/A':>10} {'N/A':>10} MISSING"
            )
            continue

        curr_s = curr_scenarios[key]

        # 1. Compare p95 (lower is better)
        b_p95 = base_s.get("p95_ms", 0.0)
        c_p95 = curr_s.get("p95_ms", 0.0)
        if b_p95 > 0:
            diff_p95_pct = (c_p95 - b_p95) / b_p95 * 100.0
            is_regress = c_p95 > b_p95 * (1.0 + threshold_fraction)
            status = "REGRESS" if is_regress else "OK"
            if is_regress:
                regressions.append(
                    f"{label} (c={conc}) p95 worsened by {diff_p95_pct:+.1f}% "
                    f"({b_p95:.1f}ms -> {c_p95:.1f}ms)"
                )
            print(
                f"{label:<30} {conc:<3} {'p95_ms':<8} {b_p95:>9.1f}ms {c_p95:>9.1f}ms "
                f"{diff_p95_pct:>+9.1f}% {status:<10}"
            )

        # 2. Compare RPS (higher is better)
        b_rps = base_s.get("rps", 0.0)
        c_rps = curr_s.get("rps", 0.0)
        if b_rps > 0:
            diff_rps_pct = (c_rps - b_rps) / b_rps * 100.0
            is_regress = c_rps < b_rps * (1.0 - threshold_fraction)
            status = "REGRESS" if is_regress else "OK"
            if is_regress:
                regressions.append(
                    f"{label} (c={conc}) RPS dropped by {abs(diff_rps_pct):.1f}% "
                    f"({b_rps:.1f} -> {c_rps:.1f})"
                )
            print(
                f"{label:<30} {conc:<3} {'rps':<8} {b_rps:>10.2f} {c_rps:>10.2f} "
                f"{diff_rps_pct:>+9.1f}% {status:<10}"
            )

    print("-" * len(header))
    if regressions:
        print(f"\n[FAIL] {len(regressions)} regression(s) exceeded {args.max_regress}% threshold:")
        for r in regressions:
            print(f"  - {r}")
        return 1

    print("\n[PASS] No performance regression detected within allowed threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
