#!/usr/bin/env python3
"""MetaSearch Automated Version Management, Quality Gate & Release Pipeline.

Enforces:
1. Workspace Hygiene: cleans temp files, test databases, caches, next.js artifacts.
2. Quality Gates: Ruff formatting/linting, Bandit AST scan, Pytest suite, Next.js build.
3. SemVer Synchronization: atomically bumps api/__init__.py, pyproject.toml, package.json.
4. Keep a Changelog Verification: validates entry for the target version in CHANGELOG.md.
5. Git Packaging: stages clean files, creates release commit, and creates annotated Git tag.

Usage:
    python scripts/release.py --clean-only
    python scripts/release.py --check
    python scripts/release.py --bump patch [--push]
    python scripts/release.py --bump minor [--push]
    python scripts/release.py --version 1.0.1 [--push]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
from contextlib import suppress
from pathlib import Path

# Force UTF-8 encoding on standard streams to avoid Windows charmap errors on Vietnamese text
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

SEMVER_REGEX = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<buildmetadata>[0-9A-Za-z.-]+))?$"
)

# Directories to skip when scanning for temporary files
IGNORE_SCAN_DIRS = {".git", ".venv", "venv", "node_modules"}


def log(msg: str, *, prefix: str = "->", level: str = "INFO") -> None:
    colors = {
        "INFO": "\033[36m",  # Cyan
        "SUCCESS": "\033[32m",  # Green
        "WARN": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "RESET": "\033[0m",
    }
    col = colors.get(level, "")
    rst = colors["RESET"]
    print(f"{col}{prefix} [{level}] {msg}{rst}")


def find_repo_root() -> Path:
    """Traverse upwards to locate repository root containing README.md and pyproject.toml."""
    current = Path(__file__).resolve().parent
    for p in [current, *current.parents]:
        if (p / "AGENTS.md").exists() and (p / "README.md").exists():
            return p
    return Path.cwd().resolve()


def find_python_executable(root: Path) -> Path:
    """Locate the development virtualenv Python executable or fallback to sys.executable."""
    candidates = [
        root / "api" / ".venv" / "Scripts" / "python.exe",  # Windows venv
        root / "api" / ".venv" / "bin" / "python",  # Linux venv
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return Path(sys.executable)


def find_npm_executable() -> str:
    """Find npm executable in PATH."""
    npm_name = "npm.cmd" if sys.platform == "win32" else "npm"
    which_npm = shutil.which(npm_name)
    if which_npm:
        return which_npm
    fallback = shutil.which("npm")
    return fallback or "npm"


# Local runtime data must NEVER be cleaned by this script either (AGENTS.md):
# SQLite DBs under api/, *.retention markers and api/.env.* (except *.example)
# hold live keys/users/history of integrated systems — deleting one locks every
# integrated client out (401) until a new key is issued (incident 2026-11-10).
_PROTECTED_SUFFIX_RE = re.compile(r"\.db(-wal|-shm)?$|\.sqlite3$|\.retention$", re.IGNORECASE)


def _is_protected_local_data(root: Path, p: Path) -> bool:
    rel = p.relative_to(root)
    if rel.parts and rel.parts[0] == "api":
        if _PROTECTED_SUFFIX_RE.search(p.name):
            return True
        if p.name.startswith(".env.") and not p.name.endswith(".example"):
            return True
    return False


def clean_workspace(root: Path, *, dry_run: bool = False) -> list[str]:
    """Clean all temporary database files, test artifacts, caches, and logs.

    Never touches protected local runtime data (see _is_protected_local_data),
    and never crashes on a file locked by a running local process.
    """
    cleaned: list[str] = []

    # 1. Target exact cache directories
    cache_dirs = [
        root / ".ruff_cache",
        root / "api" / ".ruff_cache",
        root / "api" / ".pytest_cache",
        root / "frontend" / ".next",
        root / "frontend" / "tsconfig.tsbuildinfo",
    ]
    for target in cache_dirs:
        if target.exists():
            if not dry_run:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    # locked by a running process — skip, never crash
                    with suppress(OSError):
                        target.unlink(missing_ok=True)
            cleaned.append(str(target.relative_to(root)))

    # 2. File patterns to remove across api, frontend, scripts, deploy
    temp_extensions = {".db", ".db-shm", ".db-wal", ".sqlite3", ".tmp", ".bak", ".log"}

    for dirpath, _dirnames, filenames in os.walk(root):
        rel_parts = Path(dirpath).relative_to(root).parts
        if any(ign in rel_parts for ign in IGNORE_SCAN_DIRS):
            continue

        # Clean __pycache__
        if Path(dirpath).name == "__pycache__":
            if not dry_run:
                shutil.rmtree(dirpath, ignore_errors=True)
            cleaned.append(str(Path(dirpath).relative_to(root)))
            continue

        for f in filenames:
            ext = Path(f).suffix.lower()
            if ext in temp_extensions or f.endswith(".tsbuildinfo"):
                p = Path(dirpath) / f
                if _is_protected_local_data(root, p):
                    continue  # live runtime data — never delete (AGENTS.md)
                if not dry_run:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError as exc:
                        # e.g. .lite.err.log held open by the running local server:
                        # skip with a warning instead of crashing the whole gate.
                        log(f"WARN: skip locked file {p.relative_to(root)}: {exc}", level="WARN")
                        continue
                cleaned.append(str(p.relative_to(root)))

    return cleaned


def run_command(cmd: list[str], *, cwd: Path, desc: str) -> None:
    """Execute a shell command with real-time feedback and fail-fast assertion."""
    log(f"Running: {desc} ({' '.join(cmd)}) ...", prefix="==")
    proc = subprocess.run(cmd, check=False, cwd=cwd, text=True, capture_output=True)  # nosec B603
    if proc.returncode != 0:
        log(f"FAILED: {desc}", level="ERROR")
        if proc.stdout:
            print("--- STDOUT ---\n" + proc.stdout)
        if proc.stderr:
            print("--- STDERR ---\n" + proc.stderr)
        sys.exit(1)
    log(f"PASSED: {desc}", level="SUCCESS")


def ensure_git_pre_commit_hook(root: Path) -> bool:
    """Ensure .githooks/pre-commit is registered as Git core.hooksPath and executable."""
    hook_file = root / ".githooks" / "pre-commit"
    if not hook_file.exists():
        return False
    if sys.platform != "win32":
        try:
            current_mode = hook_file.stat().st_mode
            hook_file.chmod(current_mode | 0o755)
        except OSError:
            pass
    proc = subprocess.run(  # nosec B603, B607
        ["git", "config", "core.hooksPath", ".githooks"],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _pyproject_project_version(text: str) -> str | None:
    """First `version = ...` inside the [project] table only (TOML section-aware).

    A bare `^version` regex would also match e.g. `[tool.something] version = ...`
    and produce a false mismatch — anchor to the [project] section instead.
    """
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section == "[project]":
            m = re.match(r'version\s*=\s*["\']([^"\']+)["\']', stripped)
            if m:
                return m.group(1)
    return None


def _lock_metasearch_version(text: str) -> str | None:
    """Version of the `metasearch-api` [[package]] block in uv.lock.

    Splits on package blocks and matches `name`/`version` independently of
    their order inside the block (uv currently writes name-then-version, but
    the check must not silently skip on a reordered lock).
    """
    for block in text.split("[[package]]")[1:]:
        if re.search(r'(?m)^name\s*=\s*"metasearch-api"$', block):
            m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', block)
            return m.group(1) if m else None
    return None


def check_lockfile_version(root: Path) -> None:
    """api/uv.lock must pin the same metasearch-api version as api/pyproject.toml.

    CI installs with `uv sync --locked`, which fails hard when the lockfile is
    stale — a version bump without `uv lock` must never reach a commit.
    Pure-stdlib check (no uv binary needed) so the hook works on every OS.
    Fail-closed: missing files or unparseable formats BLOCK instead of skipping,
    because a silently skipped check is exactly how stale locks reach CI.
    """
    pyproject = root / "api" / "pyproject.toml"
    lockfile = root / "api" / "uv.lock"
    if not pyproject.exists() or not lockfile.exists():
        log(
            "BLOCKED: api/pyproject.toml and api/uv.lock must both exist for the lock check",
            level="ERROR",
        )
        sys.exit(1)
    py_ver = _pyproject_project_version(pyproject.read_text(encoding="utf-8"))
    lock_ver = _lock_metasearch_version(lockfile.read_text(encoding="utf-8"))
    if py_ver is None or lock_ver is None:
        log(
            "BLOCKED: could not parse metasearch-api version from pyproject.toml/uv.lock "
            "(fail-closed — fix the files or the parser, do not skip the check)",
            level="ERROR",
        )
        sys.exit(1)
    if py_ver != lock_ver:
        log(
            f"BLOCKED: api/pyproject.toml is {py_ver} but api/uv.lock pins metasearch-api "
            f"{lock_ver}. Run `uv lock` in api/ and commit the updated uv.lock "
            "(CI `uv sync --locked` fails otherwise).",
            level="ERROR",
        )
        sys.exit(1)
    log(f"PASSED: uv.lock pins metasearch-api {lock_ver} (matches pyproject.toml)", level="SUCCESS")


def run_pre_commit_gate(root: Path, python_bin: Path) -> None:
    """Mandatory pre-commit gate: lockfile sync, Ruff, Bandit, E2E + Unit suites."""
    log(
        "Enforcing Mandatory Pre-Commit Gate (uv.lock, Ruff, Bandit, E2E + Unit)...",
        prefix=">>",
    )

    # 0. Lockfile must match pyproject version (CI `uv sync --locked` enforces it)
    check_lockfile_version(root)

    e2e_file = root / "api" / "tests" / "test_e2e_full_system.py"
    if not e2e_file.exists():
        log(
            f"BLOCKED: Mandatory E2E test suite missing at {e2e_file}!",
            level="ERROR",
        )
        sys.exit(1)

    # 1. Ruff Format Check
    run_command(
        [
            str(python_bin),
            "-m",
            "ruff",
            "format",
            "--check",
            "--config",
            "pyproject.toml",
            "app",
            "tests",
            str(root / "scripts"),
        ],
        cwd=root / "api",
        desc="Ruff Code Formatting Check",
    )

    # 2. Ruff Lint Check
    run_command(
        [
            str(python_bin),
            "-m",
            "ruff",
            "check",
            "--config",
            "pyproject.toml",
            "app",
            "tests",
            str(root / "scripts"),
        ],
        cwd=root / "api",
        desc="Ruff Static Analysis & Linting",
    )

    # 3. Bandit Security Scan
    run_command(
        [str(python_bin), "-m", "bandit", "-q", "-r", "app", "-x", "tests"],
        cwd=root / "api",
        desc="Bandit AST Security Audit",
    )

    # 4. Mandatory End-to-End (E2E) Full System Test Suite
    run_command(
        [str(python_bin), "-m", "pytest", "tests/test_e2e_full_system.py", "-q"],
        cwd=root / "api",
        desc="Mandatory End-to-End (E2E) Full System Verification Suite",
    )

    # 5. Full Pytest Unit & Integration Suite
    run_command(
        [str(python_bin), "-m", "pytest", "tests", "-q"],
        cwd=root / "api",
        desc="Pytest Backend Full Test Suite",
    )

    log("Pre-Commit Gate Passed: 100% Unit & E2E Tests Green!", level="SUCCESS")


def run_quality_gates(root: Path, python_bin: Path, npm_bin: str) -> None:
    """Enforce formatting, linting, security scans, E2E tests, unit tests, and build."""
    log(
        "Enforcing 7 Mandatory Quality Gates (Ruff, Bandit, E2E, Pytest, Next.js Build)...",
        prefix=">>",
    )

    run_pre_commit_gate(root, python_bin)

    # 6. Frontend Typecheck
    run_command(
        [npm_bin, "run", "typecheck"],
        cwd=root / "frontend",
        desc="Frontend TypeScript Strict Typecheck",
    )

    # 7. Frontend Build
    run_command(
        [npm_bin, "run", "build"],
        cwd=root / "frontend",
        desc="Next.js Production Build Validation",
    )

    log("All 7 Quality & E2E Gates Passed with Zero Errors!", level="SUCCESS")


def get_current_versions(root: Path) -> dict[str, str]:
    """Retrieve and verify current versions from all components."""
    versions: dict[str, str] = {}

    # api/app/__init__.py
    init_path = root / "api" / "app" / "__init__.py"
    if init_path.exists():
        match = re.search(
            r'__version__\s*=\s*["\']([^"\']+)["\']',
            init_path.read_text(encoding="utf-8"),
        )
        if match:
            versions["api_init"] = match.group(1)

    # api/pyproject.toml
    pyproject_path = root / "api" / "pyproject.toml"
    if pyproject_path.exists():
        match = re.search(
            r'(?m)^version\s*=\s*["\']([^"\']+)["\']',
            pyproject_path.read_text(encoding="utf-8"),
        )
        if match:
            versions["pyproject"] = match.group(1)

    # frontend/package.json
    pkg_path = root / "frontend" / "package.json"
    if pkg_path.exists():
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        versions["frontend"] = data.get("version", "")

    return versions


def calculate_bump(current_version: str, bump_type: str) -> str:
    """Calculate the next SemVer version according to bump type."""
    match = SEMVER_REGEX.match(current_version)
    if not match:
        raise ValueError(f"Current version '{current_version}' does not match SemVer format!")

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))

    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    if bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"

    raise ValueError(f"Unknown bump type: {bump_type}")


def update_version_files(root: Path, new_version: str, *, dry_run: bool = False) -> None:
    """Atomically rewrite version in all three component configuration files."""
    log(f"Synchronizing version {new_version} across codebase...", prefix=">>")

    # 1. api/app/__init__.py
    init_file = root / "api" / "app" / "__init__.py"
    content = init_file.read_text(encoding="utf-8")
    new_content = re.sub(
        r'__version__\s*=\s*["\'][^"\']+["\']',
        f'__version__ = "{new_version}"',
        content,
    )
    if not dry_run:
        init_file.write_text(new_content, encoding="utf-8")
    log(f"Updated {init_file.relative_to(root)}")

    # 2. api/pyproject.toml
    pyproject_file = root / "api" / "pyproject.toml"
    content = pyproject_file.read_text(encoding="utf-8")
    new_content = re.sub(
        r'(?m)^version\s*=\s*["\'][^"\']+["\']', f'version = "{new_version}"', content
    )
    if not dry_run:
        pyproject_file.write_text(new_content, encoding="utf-8")
    log(f"Updated {pyproject_file.relative_to(root)}")

    # 3. frontend/package.json
    pkg_file = root / "frontend" / "package.json"
    content = pkg_file.read_text(encoding="utf-8")
    new_content = re.sub(r'"version":\s*"[^"]+"', f'"version": "{new_version}"', content)
    if not dry_run:
        pkg_file.write_text(new_content, encoding="utf-8")
    log(f"Updated {pkg_file.relative_to(root)}")


def verify_changelog(root: Path, version: str) -> tuple[bool, str]:
    """Verify that CHANGELOG.md has an entry for the target version and extract release notes."""
    changelog_path = root / "CHANGELOG.md"
    if not changelog_path.exists():
        return False, "CHANGELOG.md not found!"

    text = changelog_path.read_text(encoding="utf-8")
    header_pattern = re.compile(rf"##\s*\[{re.escape(version)}\]", re.MULTILINE)
    match = header_pattern.search(text)
    if not match:
        return False, f"Missing '## [{version}]' entry in CHANGELOG.md!"

    # Extract section until next '## ['
    start_pos = match.start()
    next_header = re.search(r"(?m)^##\s*\[", text[match.end() :])
    if next_header:
        section = text[start_pos : match.end() + next_header.start()].strip()
    else:
        section = text[start_pos:].strip()

    return True, section


def git_release(root: Path, version: str, *, push: bool = False, dry_run: bool = False) -> None:
    """Stage version changes, create release commit, and tag."""
    log(f"Packaging Git Release v{version}...", prefix=">>")
    tag_name = f"v{version}"

    # Verify git status
    proc = subprocess.run(  # nosec B603, B607
        ["git", "status", "--porcelain"],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
    )
    uncommitted = proc.stdout.strip()
    if uncommitted:
        log("Staging modified version files and clean tree...", prefix="->")
        if not dry_run:
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)  # nosec B603, B607

    commit_msg = f"release: v{version}"
    if dry_run:
        log(f"[DRY-RUN] Would create git commit: '{commit_msg}'")
        log(f"[DRY-RUN] Would create git tag: '{tag_name}'")
        if push:
            log("[DRY-RUN] Would push to origin with tags")
        return

    # Check if anything to commit
    proc = subprocess.run(  # nosec B603, B607
        ["git", "status", "--porcelain"],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=root, check=True)  # nosec B603, B607
        log(f"Created release commit: {commit_msg}", level="SUCCESS")
    else:
        log("No changes to commit (already up to date).", level="WARN")

    # Check if tag already exists
    tag_check = subprocess.run(  # nosec B603, B607
        ["git", "tag", "-l", tag_name],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
    )
    if tag_check.stdout.strip() == tag_name:
        log(f"Tag {tag_name} already exists. Skipping tag creation.", level="WARN")
    else:
        subprocess.run(  # nosec B603, B607
            ["git", "tag", "-a", tag_name, "-m", f"MetaSearch Release {tag_name}"],
            cwd=root,
            check=True,
        )
        log(f"Created annotated tag: {tag_name}", level="SUCCESS")

    if push:
        log(f"Pushing commit and tag {tag_name} to origin...", prefix=">>")
        subprocess.run(  # nosec B603, B607
            ["git", "push", "origin", "main", "--follow-tags"], cwd=root, check=True
        )
        log("Successfully pushed release to remote!", level="SUCCESS")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MetaSearch Automated Version Management, Quality Gate & Release Pipeline"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bump", choices=["patch", "minor", "major"], help="SemVer increment type")
    group.add_argument("--version", help="Explicit target version string (e.g. 1.0.1)")
    group.add_argument(
        "--clean-only",
        action="store_true",
        help="Clean temp files and caches only without release",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="Run clean, quality gates, and version sanity check",
    )
    group.add_argument(
        "--pre-commit-gate",
        action="store_true",
        help="Run mandatory pre-commit verification (Ruff, Bandit, E2E Suite, Unit Suite)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate release steps without modifying disk or Git",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip running test suites (emergency only)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push release commit and tags to origin remote",
    )
    parser.add_argument(
        "--notes",
        action="store_true",
        help="Print changelog release notes for current or target version",
    )

    args = parser.parse_args()
    root = find_repo_root()
    python_bin = find_python_executable(root)
    npm_bin = find_npm_executable()

    log(f"MetaSearch Repository Root: {root}")
    log(f"Python Runtime: {python_bin}")
    log(f"NPM Runtime: {npm_bin}")

    ensure_git_pre_commit_hook(root)

    if args.pre_commit_gate:
        run_pre_commit_gate(root, python_bin)
        return

    # 1. Clean workspace
    log("Cleaning temporary files, test databases, and build caches...", prefix=">>")
    cleaned_items = clean_workspace(root, dry_run=args.dry_run)
    log(f"Cleaned {len(cleaned_items)} files/directories.", level="SUCCESS")
    if args.clean_only:
        return

    # 2. Check current versions
    versions = get_current_versions(root)
    cur_ver = versions.get("api_init", "1.0.0")
    log(
        f"Current Versions: api={versions.get('api_init')}, "
        f"pyproject={versions.get('pyproject')}, "
        f"frontend={versions.get('frontend')}"
    )

    if len(set(versions.values())) > 1:
        log(f"Version mismatch detected across files: {versions}", level="WARN")

    if args.notes:
        target_v = args.version or cur_ver
        ok, section = verify_changelog(root, target_v)
        if ok:
            print(f"\n=== Release Notes for [{target_v}] ===\n{section}\n")
        else:
            log(section, level="ERROR")
        return

    # Determine target version
    target_version = cur_ver
    if args.bump:
        target_version = calculate_bump(cur_ver, args.bump)
    elif args.version:
        if not SEMVER_REGEX.match(args.version):
            log(f"Invalid SemVer string: {args.version}", level="ERROR")
            sys.exit(1)
        target_version = args.version

    log(f"Target Version: {target_version} (Current: {cur_ver})", prefix="==")

    # 3. Verify CHANGELOG.md first
    ok, notes = verify_changelog(root, target_version)
    if not ok:
        log(notes, level="ERROR")
        log(
            f"Please add a release entry for '## [{target_version}]' in CHANGELOG.md!",
            level="ERROR",
        )
        sys.exit(1)
    else:
        log(f"Changelog verified for version {target_version}!", level="SUCCESS")

    # 4. Apply version updates if bumping (before running tests so exact bumped code is verified)
    if target_version != cur_ver and not args.check:
        update_version_files(root, target_version, dry_run=args.dry_run)

    # 5. Mandatory Quality & E2E Gates (Strict: commit is NEVER allowed if tests are skipped)
    if args.skip_tests:
        if not args.check and not args.dry_run:
            log(
                "BLOCKED: Commit/Release is strictly forbidden when --skip-tests is set! "
                "All unit and E2E tests must run and pass with 0 errors before committing.",
                level="ERROR",
            )
            sys.exit(1)
        log(
            "Quality gates skipped via --skip-tests flag (check/dry-run only).",
            level="WARN",
        )
    else:
        run_quality_gates(root, python_bin, npm_bin)

    if args.check:
        log("Pre-flight check & Quality Gates successfully completed!", level="SUCCESS")
        return

    # 6. Final Clean
    clean_workspace(root, dry_run=args.dry_run)

    # 7. Git commit, tag, and optional push
    git_release(root, target_version, push=args.push, dry_run=args.dry_run)

    log(f"MetaSearch Version {target_version} successfully processed!", level="SUCCESS")


if __name__ == "__main__":
    main()
