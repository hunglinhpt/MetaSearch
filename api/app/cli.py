"""``metasearch`` console script.

Phase 0 exposes ``--version`` only; ``search``/``answer``/``research``
subcommands arrive with Phase 1 (docs/12-roadmap-plan.md).
"""

from __future__ import annotations

import argparse
import sys

from app import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metasearch", description="MetaSearch CLI")
    parser.add_argument("--version", action="version", version=f"metasearch {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
