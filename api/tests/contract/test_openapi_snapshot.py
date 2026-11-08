"""The /v1 contract is pinned by a snapshot; any change to it must be deliberate.

Regenerate after an intended change:
    METASEARCH_UPDATE_OPENAPI=1 python -m pytest tests/contract -q
and review the diff of tests/contract/openapi_v1.json like any other contract change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).with_name("openapi_v1.json")


def _v1_contract() -> dict:
    spec = app.openapi()
    paths = {p: v for p, v in spec.get("paths", {}).items() if p.startswith("/v1/")}
    return {
        "openapi": spec.get("openapi"),
        "paths": paths,
        "components": spec.get("components", {}),
    }


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_openapi_v1_matches_snapshot():
    current = _dump(_v1_contract())
    if os.environ.get("METASEARCH_UPDATE_OPENAPI") == "1":
        SNAPSHOT.write_text(current, encoding="utf-8")
    assert SNAPSHOT.exists(), "snapshot missing: run with METASEARCH_UPDATE_OPENAPI=1"
    expected = SNAPSHOT.read_text(encoding="utf-8")
    assert current == expected, (
        "The /v1 OpenAPI contract changed. If intended, regenerate the snapshot "
        "(METASEARCH_UPDATE_OPENAPI=1) and note the change in docs/15-integration-guide.md."
    )


def test_snapshot_documents_the_contract_fields():
    spec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    search_req = schemas["SearchRequest"]["properties"]
    assert "include_domains" in search_req and "exclude_domains" in search_req
    assert "include" in schemas["ContextRequest"]["properties"]
    assert "/v1/search" in spec["paths"] and "/v1/context" in spec["paths"]
