"""Structured JSON output from search results (docs/20 P2-6 — Exa-style
``response_format={json_schema}`` for agents).

The LLM extracts an object matching the caller's JSON schema from the top
results; the object is then validated with ``jsonschema`` (pinned dependency).
Validation failure is reported, never silently rewritten: the caller gets
``structured: null`` plus the validator errors, and the raw LLM text stays in
the log — same "no fabrication" rule as citations.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def validate_instance(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a pinned dep
        return ["jsonschema not installed"]
    try:
        jsonschema.validate(instance=instance, schema=schema)
        return []
    except jsonschema.ValidationError as exc:
        return [f"{list(exc.absolute_path)}: {exc.message}"]
    except jsonschema.SchemaError as exc:
        return [f"schema: {exc.message}"]
    # Audit Pass 1 P1 (round 14): caller-supplied schemas can raise
    # _WrappedReferencingError (unresolvable $ref) or RecursionError
    # (recursive schema + deep LLM JSON) — both escaped the two catches
    # above and 500'd the whole search, breaking the documented fail-open.
    except Exception as exc:
        return [f"validator: {type(exc).__name__}: {exc}"][:1]


def check_schema(schema: dict[str, Any]) -> str | None:
    """Cheap static check at request entry (Pass 1 P3#9): reject malformed
    schemas BEFORE burning the paid LLM call. Returns an error message or
    None when the schema is usable."""
    try:
        import jsonschema as _js

        _js.validators.validator_for(schema).check_schema(schema)
        return None
    except Exception as exc:
        return f"schema: {type(exc).__name__}: {exc}"[:200]


def _schema_prompt(query: str, schema: dict[str, Any], context: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You extract structured data from web search results. "
                "Reply with ONE JSON object and nothing else. It MUST validate "
                "against the given JSON schema. If the results do not contain a "
                "field, use null (or the schema default) — never invent values. "
                "Every factual field should be traceable to the context."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Query: {query}\n\nJSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Search results (title — url — snippet):\n{context[:6000]}"
            ),
        },
    ]


async def extract_structured(
    gateway: Any,
    query: str,
    schema: dict[str, Any],
    results: list[Any],
    *,
    model: str,
    max_tokens: int = 1200,
) -> tuple[dict[str, Any] | None, list[str], str]:
    """Extract + validate. Returns (object_or_None, validation_errors, raw_text)."""
    context = "\n".join(f"- {r.title} — {r.url} — {(r.snippet or '')[:300]}" for r in results[:10])
    try:
        text, _usage = await gateway.complete(
            _schema_prompt(query, schema, context),
            model=model,
            max_tokens=max_tokens,
            task="structured",
        )
    except Exception as exc:
        logger.warning("structured_extract_llm_failed", error=type(exc).__name__)
        return None, [f"llm: {type(exc).__name__}"], ""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return None, ["no JSON object in LLM reply"], text or ""
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return None, [f"json: {exc}"], text or ""
    errors = validate_instance(obj, schema)
    if errors:
        logger.info("structured_validation_failed", errors=errors[:3])
        return None, errors, text or ""
    return obj, [], text or ""
