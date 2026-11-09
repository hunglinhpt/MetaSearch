"""The typed surface stays clean: mypy over the files configured in pyproject.toml."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mypy")

API = Path(__file__).resolve().parents[1]


def test_mypy_is_clean_on_the_typed_surface():
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental"],
        cwd=API,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
