# MetaSearch API (v1.0.0)

Thin orchestration and quantitative intelligence layer of MetaSearch. SearXNG owns general meta-search (fan-out over 70+ engines) and crawl4ai / trafilatura / Jina Reader own extraction, while this service adds 14 specialized vertical adapters, institutional financial forensics, quantitative technical analysis, smart category routing, multi-signal ranking, citation-validated synthesis, caching, SSE streaming, automated daily briefings, and the MCP surface.

See [`../docs/02-system-architecture.md`](../docs/02-system-architecture.md) for the full design and [`../docs/12-roadmap-plan.md`](../docs/12-roadmap-plan.md) for the phase plan.

## Layout

```
app/
  api/            HTTP surface (ops + /v1: search, answer, scrape, research, digests, engines, queries, ops_status)
  core/           auth (API key + RBAC + sliding-window rate limit), errors, logging, middleware, metrics
  db/             SQLAlchemy dialect-aware models (Postgres+pgvector & SQLite) + async session factory
  retrieval/      19 search adapters, financial_forensics.py, technical_analysis.py, registry, merger, scoring, simhash, reranker
  extraction/     trafilatura, crawl4ai client, Jina Reader, SSRF guard, IP pinning, robots.txt, chunker
  orchestration/  pipeline (search+answer), research (Harness² 8 profiles), research_jobs, scheduler (Daily Briefings), persist, engine_store
  mcp/            FastMCP server + client (4 tools: web_search, web_scrape, web_search_and_scrape, deep_research)
  llm/            provider-agnostic chat + embedding gateway, prompts, citations, tokens, Langfuse tracing
alembic/          schema migrations
evals/            40 golden evaluation queries (EN + VI)
scripts/          eval_run.py (--judge-faithfulness) + perf_load.py
tests/            333 unit tests + 7 integration tests (no live Postgres/Redis required for unit suite)
```

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\python.exe -m bandit -q -r app -x tests
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

`GET /healthz` is liveness, `GET /readyz` checks Postgres/SQLite + Redis + SearXNG (Lite-profile aware), `GET /metrics` exposes Prometheus metrics, and `GET /v1/meta` reports the active giants configuration.

## Migrations

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe change"
```

`alembic/env.py` reads `DATABASE_URL` through `app.config.get_settings()`, so the same `.env` used by the API drives migrations. In Lite mode (`sqlite+aiosqlite`), schema tables are automatically bootstrapped at startup.
