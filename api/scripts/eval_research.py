"""Run offline Deep Research FACT benchmark against research_golden.json."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval_research import DEFAULT_RESEARCH_GOLDEN, evaluate_research_golden

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", type=Path, default=DEFAULT_RESEARCH_GOLDEN)
    args = parser.parse_args()
    summary = evaluate_research_golden(args.golden)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
