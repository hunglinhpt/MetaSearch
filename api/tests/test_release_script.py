"""Unit tests for MetaSearch Version Management & Release Pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to sys.path so we can import scripts.release
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.release import (  # noqa: E402
    SEMVER_REGEX,
    calculate_bump,
    clean_workspace,
    find_npm_executable,
    find_python_executable,
    find_repo_root,
    get_current_versions,
    verify_changelog,
)


def test_semver_regex_validation():
    assert SEMVER_REGEX.match("1.0.0")
    assert SEMVER_REGEX.match("0.1.0-alpha.1")
    assert SEMVER_REGEX.match("2.14.8+build.123")
    assert not SEMVER_REGEX.match("v1.0.0")
    assert not SEMVER_REGEX.match("1.0")
    assert not SEMVER_REGEX.match("beta-1.0")


def test_calculate_bump():
    assert calculate_bump("1.0.0", "patch") == "1.0.1"
    assert calculate_bump("1.0.0", "minor") == "1.1.0"
    assert calculate_bump("1.0.0", "major") == "2.0.0"
    assert calculate_bump("0.6.9", "patch") == "0.6.10"
    assert calculate_bump("0.6.9", "minor") == "0.7.0"


def test_find_repo_root_and_executables():
    root = find_repo_root()
    assert (root / "README.md").exists()
    assert (root / "AGENTS.md").exists()

    python_bin = find_python_executable(root)
    assert python_bin.exists()

    npm_bin = find_npm_executable()
    assert npm_bin != ""


def test_get_current_versions():
    root = find_repo_root()
    versions = get_current_versions(root)
    assert "api_init" in versions
    assert "pyproject" in versions
    assert "frontend" in versions
    # All components should be at 1.0.0
    assert versions["api_init"] == "1.0.0"
    assert versions["pyproject"] == "1.0.0"
    assert versions["frontend"] == "1.0.0"


def test_verify_changelog():
    root = find_repo_root()
    ok, section = verify_changelog(root, "1.0.0")
    assert ok is True
    assert "## [1.0.0]" in section
    assert "Institutional Financial Forensics" in section

    not_ok, msg = verify_changelog(root, "99.99.99")
    assert not_ok is False
    assert "Missing '## [99.99.99]'" in msg


def test_clean_workspace_dry_run(tmp_path):
    # Create fake directory structure with temp files
    test_dir = tmp_path / "repo"
    test_dir.mkdir()
    (test_dir / "test.db").write_text("data", encoding="utf-8")
    (test_dir / "test.tmp").write_text("data", encoding="utf-8")
    pycache = test_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "file.pyc").write_text("bin", encoding="utf-8")

    cleaned = clean_workspace(test_dir, dry_run=True)
    assert len(cleaned) >= 2
    # Files should still exist in dry_run
    assert (test_dir / "test.db").exists()

    # Actual clean
    cleaned_real = clean_workspace(test_dir, dry_run=False)
    assert len(cleaned_real) >= 2
    assert not (test_dir / "test.db").exists()
    assert not (test_dir / "test.tmp").exists()
    assert not pycache.exists()
