"""Unit tests for MetaSearch Version Management & Release Pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add repo root to sys.path so we can import scripts.release
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.release import (  # noqa: E402
    SEMVER_REGEX,
    _lock_metasearch_version,
    _pyproject_project_version,
    calculate_bump,
    check_lockfile_version,
    clean_workspace,
    ensure_git_pre_commit_hook,
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
    assert SEMVER_REGEX.match(versions["api_init"])
    assert versions["api_init"] == versions["pyproject"]
    assert versions["api_init"] == versions["frontend"]


def test_mandatory_e2e_and_pre_commit_hook_present():
    root = find_repo_root()
    assert (root / ".githooks" / "pre-commit").exists()
    assert (root / "api" / "tests" / "test_e2e_full_system.py").exists()
    assert ensure_git_pre_commit_hook(root) is True


def test_verify_changelog():
    root = find_repo_root()
    ok, section = verify_changelog(root, "1.0.0")
    assert ok is True
    assert "## [1.0.0]" in section
    assert "Institutional Financial Forensics" in section

    not_ok, msg = verify_changelog(root, "99.99.99")
    assert not_ok is False
    assert "Missing '## [99.99.99]'" in msg


def _write_fake_repo(tmp_path: Path, pyproject: str, lock: str | None) -> Path:
    api_dir = tmp_path / "api"
    api_dir.mkdir(exist_ok=True)
    (api_dir / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if lock is not None:
        (api_dir / "uv.lock").write_text(lock, encoding="utf-8")
    return tmp_path


_PYPROJECT = '[project]\nname = "metasearch-api"\nversion = "2.2.0"\n'
_LOCK = (
    'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\n'
    'name = "metasearch-api"\nversion = "2.2.0"\n'
)


def test_pyproject_version_reads_project_section_only():
    # A version under another TOML section must not be picked up.
    text = '[tool.other]\nversion = "0.0.1"\n\n[project]\nname = "x"\nversion = "2.2.0"\n'
    assert _pyproject_project_version(text) == "2.2.0"
    assert _pyproject_project_version("[project]\nname = 'x'\n") is None


def test_lock_version_order_independent():
    assert _lock_metasearch_version(_LOCK) == "2.2.0"
    reordered = '[[package]]\nversion = "9.9.9"\nname = "metasearch-api"\n'
    assert _lock_metasearch_version(reordered) == "9.9.9"
    assert _lock_metasearch_version('[[package]]\nname = "other"\nversion = "1.0.0"\n') is None


def test_check_lockfile_version_real_repo_passes():
    check_lockfile_version(find_repo_root())  # must not raise SystemExit


def test_check_lockfile_version_mismatch_blocks(tmp_path):
    root = _write_fake_repo(tmp_path, _PYPROJECT, _LOCK.replace("2.2.0", "2.0.0", 1))
    with pytest.raises(SystemExit) as exc:
        check_lockfile_version(root)
    assert exc.value.code == 1


def test_check_lockfile_version_fail_closed(tmp_path):
    # Missing lockfile -> BLOCK, not a silent skip.
    root = _write_fake_repo(tmp_path, _PYPROJECT, None)
    with pytest.raises(SystemExit):
        check_lockfile_version(root)
    # Unparseable lock (no metasearch-api package) -> BLOCK.
    root = _write_fake_repo(tmp_path, _PYPROJECT, '[[package]]\nname = "other"\nversion = "1"\n')
    with pytest.raises(SystemExit):
        check_lockfile_version(root)
    # pyproject without a [project] version -> BLOCK.
    root = _write_fake_repo(tmp_path, "[project]\nname = 'x'\n", _LOCK)
    with pytest.raises(SystemExit):
        check_lockfile_version(root)


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


def test_clean_workspace_never_touches_protected_local_data(tmp_path):
    # AGENTS.md: api/*.db(+wal/shm), *.retention and api/.env.* (except *.example)
    # are LIVE runtime data of integrated systems — clean_workspace must skip them
    # even on a real (non-dry) run.
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    protected = [
        api_dir / "metasearch_lbs.db",
        api_dir / "metasearch_lbs.db-wal",
        api_dir / "metasearch_lbs.db-shm",
        api_dir / "metasearch_lbs.retention",
        api_dir / ".env.lbs",
        api_dir / ".env.lite.example",
    ]
    for f in protected:
        f.write_text("live", encoding="utf-8")
    (api_dir / "junk.log").write_text("tmp", encoding="utf-8")

    clean_workspace(tmp_path, dry_run=False)

    for f in protected:
        assert f.exists(), f"protected local data was deleted: {f}"
    assert not (api_dir / "junk.log").exists()  # ordinary temp files still cleaned
