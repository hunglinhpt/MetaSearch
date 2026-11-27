"""CLI for the eval regression gate (docs/36 S2).

    python scripts/eval_gate.py --baseline evals/baselines/eval_v2_golden_v2.lite.json \
        --report evals/reports/eval_v2_<timestamp>.json

Exit 0 when no gated metric dropped by more than --max-drop, 1 on a
regression, 2 on unusable input (different golden set, missing summary).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval_gate import DEFAULT_MAX_DROP, DEFAULT_METRICS, compare, load_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when eval metrics regress vs a baseline")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-drop", type=float, default=DEFAULT_MAX_DROP)
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    args = parser.parse_args(argv)
    try:
        result = compare(
            load_report(args.baseline),
            load_report(args.report),
            metrics=tuple(m.strip() for m in args.metrics.split(",") if m.strip()),
            max_drop=args.max_drop,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
