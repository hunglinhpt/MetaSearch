"""Per-Space knowledge graph (docs/20 P2-2 — LightRAG-lite, v3.0).

Entity/relation extraction runs through the cheap LLM gateway (one bounded call
per document, JSON-validated, capped node count). Storage is two tables
(``kg_nodes``/``kg_edges``) scoped by space. Query mode is entity-centric
(LightRAG "local"): match query terms against node labels (diacritic-folded),
collect the 1-hop subgraph, optionally answer over it with the LLM. GraphRAG
community summaries ("global" mode) are deliberately left out — GraphRAG-Bench
shows they only win on corpus-summary questions, and they cost the most.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.logging import get_logger
from app.retrieval.scoring import fold_diacritics, tokenize

logger = get_logger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

_EXTRACT_SYSTEM = (
    "You build a knowledge graph from a document. Reply with ONE JSON object: "
    '{"nodes": [{"label": str, "type": str, "description": str}], '
    '"edges": [{"source": str, "target": str, "relation": str}]}. '
    "Labels are short canonical entity names (lowercase, folded). Max ~15 nodes, "
    "~20 edges. No duplicates. Nothing invented — only what the document states."
)


def _extract_messages(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": text[:8000]},
    ]


def parse_graph_json(text: str, *, max_nodes: int = 200) -> dict[str, list[dict]]:
    """Parse + sanitize the LLM's graph JSON. Never raises."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return {"nodes": [], "edges": []}
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"nodes": [], "edges": []}
    raw_nodes = obj.get("nodes")
    raw_edges = obj.get("edges")
    # Pass 1 P2#5: type-confused LLM JSON ({"nodes": "abc"} / null / dict) must
    # return empty — the function's contract is "never raises".
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    if not isinstance(raw_edges, list):
        raw_edges = []
    nodes: list[dict] = []
    seen: set[str] = set()
    for n in raw_nodes[:max_nodes]:
        if not isinstance(n, dict):
            continue
        label = fold_diacritics(str(n.get("label") or "").strip().lower())[:120]
        if not label or label in seen:
            continue
        seen.add(label)
        nodes.append(
            {
                "label": label,
                "type": str(n.get("type") or "")[:60],
                "description": str(n.get("description") or "")[:500],
            }
        )
    edges: list[dict] = []
    for e in raw_edges[: max_nodes * 2]:
        if not isinstance(e, dict):
            continue
        s = fold_diacritics(str(e.get("source") or "").strip().lower())[:120]
        t = fold_diacritics(str(e.get("target") or "").strip().lower())[:120]
        r = (
            fold_diacritics(str(e.get("relation") or "related_to").strip().lower())[:80]
            or "related_to"
        )
        if s and t and s in seen and t in seen and s != t:
            edges.append({"source": s, "target": t, "relation": r})
    return {"nodes": nodes, "edges": edges}


async def extract_graph(gateway: Any, text: str, *, model: str, max_nodes: int) -> dict:
    try:
        raw, _usage = await gateway.complete(
            _extract_messages(text), model=model, max_tokens=900, task="kg_extract"
        )
        return parse_graph_json(raw, max_nodes=max_nodes)
    except Exception as exc:
        logger.warning("kg_extract_failed", error=type(exc).__name__)
        return {"nodes": [], "edges": []}


def _insert(session: Any):
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return sqlite_insert
    return pg_insert


async def build_space_graph(
    session: Any,
    space: Any,
    gateway: Any,
    *,
    settings: Any,
    max_docs: int,
    max_nodes: int,
    model: str,
) -> dict[str, int]:
    """Extract graph triples from the space's crawled documents (bounded) and
    upsert them into kg_nodes/kg_edges. Returns counts."""
    from app.db.models import Document, KgEdge, KgNode

    domains = [d.lower().removeprefix("www.") for d in (space.domains or [])]
    docs: list[Document] = []
    if domains:
        # Pass 1 P2#4: match the space-search scope semantics (exact host OR
        # any subdomain) — exact-match only left the KG partial relative to
        # the space's own search scope.
        like = [f"%.{d}" for d in domains]
        stmt = select(Document).where(
            or_(Document.domain.in_(domains), *[Document.domain.like(p) for p in like])
        )
        rows = (
            (await session.execute(stmt.order_by(Document.fetched_at.desc()).limit(max_docs)))
            .scalars()
            .all()
        )
        docs = list(rows)

    # Pass 1 P1: run ALL LLM extraction FIRST with zero DB writes open — the
    # old loop held a write transaction (SQLite write lock / PG idle-in-txn)
    # across up to 20 sequential LLM calls. Then one short bulk transaction.
    n_docs = 0
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    node_rows: list[dict[str, Any]] = []
    edge_rows: list[dict[str, Any]] = []
    for doc in docs:
        if not doc.content_md:
            continue
        n_docs += 1
        graph = await extract_graph(gateway, doc.content_md, model=model, max_nodes=max_nodes)
        for node in graph["nodes"]:
            if node["label"] in seen_nodes:
                continue  # dedupe within one build (unique constraint dedupes across builds)
            # Pass 1 P2#2: enforce the BUILD-WIDE budget (the per-doc cap let
            # one build write 20x200 nodes).
            if len(seen_nodes) >= max_nodes:
                break
            seen_nodes.add(node["label"])
            node_rows.append(
                {
                    "space_id": space.id,
                    "owner": space.api_key_id,
                    "label": node["label"],
                    "type": node["type"] or None,
                    "description": node["description"] or None,
                }
            )
        for edge in graph["edges"]:
            key = (edge["source"], edge["target"], edge["relation"])
            if key in seen_edges:
                continue
            if edge["source"] not in seen_nodes or edge["target"] not in seen_nodes:
                continue  # edge references a node dropped by the budget cap
            seen_edges.add(key)
            edge_rows.append(
                {
                    "space_id": space.id,
                    "source": edge["source"],
                    "target": edge["target"],
                    "relation": edge["relation"],
                }
            )
        if len(seen_nodes) >= max_nodes:
            break

    ins = _insert(session)
    for row in node_rows:
        await session.execute(
            ins(KgNode).values(**row).on_conflict_do_nothing(index_elements=["space_id", "label"])
        )
    for row in edge_rows:
        await session.execute(
            ins(KgEdge)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["space_id", "source", "target", "relation"])
        )
    await session.commit()
    return {"docs": n_docs, "nodes": len(node_rows), "edges": len(edge_rows)}


async def query_graph(
    session: Any, space: Any, query: str, *, limit_nodes: int = 25, limit_edges: int = 60
) -> dict[str, Any]:
    """Local (entity-centric) graph query: match folded query tokens against
    node labels, then collect the 1-hop subgraph."""
    from app.db.models import KgEdge, KgNode

    q_terms = [t for t in tokenize(query) if len(t) > 2]
    # Pass 1 P2#3: bound the load in SQL (ORDER BY recency + LIMIT) instead of
    # loading the space's ENTIRE graph into Python on every query.
    rows = (
        (
            await session.execute(
                select(KgNode)
                .where(KgNode.space_id == space.id)
                .order_by(KgNode.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    matched: list[KgNode] = []
    for node in rows:
        label = node.label or ""
        if any(term in label for term in q_terms) or label in query.lower():
            matched.append(node)
        if len(matched) >= limit_nodes:
            break
    if not matched and rows:
        # Fallback: top nodes by recency when nothing matched lexically.
        matched = list(rows[:5])
    labels = {n.label for n in matched}
    edge_rows = (
        (
            await session.execute(
                select(KgEdge)
                .where(KgEdge.space_id == space.id)
                .order_by(KgEdge.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    edges = [e for e in edge_rows if (e.source in labels or e.target in labels)][:limit_edges]
    # Include neighbor nodes of matched edges.
    neighbor_labels = set(labels)
    for e in edges:
        neighbor_labels.add(e.source)
        neighbor_labels.add(e.target)
    all_nodes = {n.label: n for n in rows}
    nodes_out = [
        {
            "label": n.label,
            "type": n.type or "",
            "description": n.description or "",
        }
        for label in sorted(neighbor_labels)
        if (n := all_nodes.get(label)) is not None
    ][: limit_nodes * 2]
    context = "\n".join(
        [f"- {n['label']} ({n['type']}): {n['description']}" for n in nodes_out]
        + [f"- {e.source} --[{e.relation}]--> {e.target}" for e in edges]
    )
    return {
        "nodes": nodes_out,
        "edges": [{"source": e.source, "target": e.target, "relation": e.relation} for e in edges],
        "context": context[:8000],
        "matched_labels": sorted(labels),
    }
