"""Shared JSON extraction helpers for LLM replies."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _try_parse_dict(raw: str) -> dict[str, Any] | None:
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from LLM prose, markdown fences, or nested braces."""
    clean_text = (text or "").strip()
    if not clean_text:
        return None

    # 1. Direct parse when the full reply is a single object
    if clean_text.startswith("{") and clean_text.endswith("}"):
        direct = _try_parse_dict(clean_text)
        if direct is not None:
            return direct

    # 2. Markdown code-fence extraction (```json ... ``` or ``` ... ```)
    for match in _FENCE_RE.finditer(clean_text):
        fenced = _try_parse_dict(match.group(1))
        if fenced is not None:
            return fenced

    # 3. String-aware balanced brace extraction
    start_idx = clean_text.find("{")
    while start_idx != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start_idx, len(clean_text)):
            ch = clean_text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        balanced = _try_parse_dict(clean_text[start_idx : idx + 1])
                        if balanced is not None:
                            return balanced
                        break
        start_idx = clean_text.find("{", start_idx + 1)

    return None
