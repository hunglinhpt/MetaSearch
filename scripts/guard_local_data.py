"""Claude Code PreToolUse hook: deleting local runtime data needs the owner's confirmation.

The SQLite databases under ``api/`` (and their -wal/-shm files, retention markers) and the
``api/.env.*`` files are live data of the systems that integrate with this repo: API keys,
users, query history. They are gitignored, so they look like temp files, but deleting one
locks every integrated client out (401) until a new key is issued and redeployed.

This hook does not block: for a shell command that would delete, move away or ``git clean``
such files, or revoke an API key, it returns ``permissionDecision: "ask"`` so the owner has
to confirm in the UI. Everything else passes through untouched. Reads the hook JSON on stdin.
"""

from __future__ import annotations

import json
import re
import sys

# Files that hold live local data (names or globs as they appear in a command).
PROTECTED = re.compile(
    r"""(\.db(-wal|-shm)?\b|\.sqlite3\b|\.retention\b|\.env\.(?![\w.-]*example\b)[\w.-]+|\bapi[\\/]+\*)""",
    re.IGNORECASE,
)
# Commands that delete or move files away.
DELETE_VERB = re.compile(
    r"""(\brm\b|\bdel\b|\berase\b|\brmdir\b|\brd\b|\bunlink\b|Remove-Item|\bri\b|\bmv\b|Move-Item|"""
    r"""\bren(ame)?\b|os\.remove|os\.unlink|shutil\.(rmtree|move)|\.unlink\(|Clear-Content|truncate)""",
    re.IGNORECASE,
)
GIT_CLEAN_IGNORED = re.compile(r"\bgit\s+clean\b[^\n|;&]*\s-[a-zA-Z]*[xX]", re.IGNORECASE)
KEY_REVOKE = re.compile(r"\bkeys\s+(revoke|delete|disable)\b", re.IGNORECASE)


def reason_for(command: str) -> str | None:
    """Why this command needs the owner's confirmation, or None when it does not."""
    if GIT_CLEAN_IGNORED.search(command):
        return "git clean -x/-X also removes ignored files: the local databases and api/.env.*"
    if KEY_REVOKE.search(command):
        return "revoking an API key cuts an integrated client off (401)"
    if DELETE_VERB.search(command) and PROTECTED.search(command):
        return (
            "this deletes or moves local runtime data "
            "(SQLite DB / -wal / -shm / .retention / api/.env.*)"
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    why = reason_for(command)
    if why is None:
        return 0
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"Owner confirmation required: {why}. These files are live data of integrated "
                    "systems, not temp files (AGENTS.md, 'Local runtime data')."
                ),
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
