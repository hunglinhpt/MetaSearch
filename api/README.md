# MetaSearch API

Thin orchestration layer of MetaSearch. It does **not** crawl the web itself:
SearXNG owns meta-search (fan-out over 70+ engines, dedup, consensus) and
crawl4ai / trafilatura own extraction. This service adds query understanding,
ranking, citation-validated synthesis, caching, SSE streaming and the MCP
surface.

See [`../docs/02-system-architecture.md`](../docs/02-system-architecture.md) for
the full design and [`../docs/12-roadmap-plan.md`](../docs/12-roadmap-plan.md)
for the phase plan.

## Layout

```
app/
  api/            HTTP surface (ops + /v1)
  core/           config-adjacent plumbing: errors, logging, middleware, metrics
  db/             SQLAlchemy models + async session factory
  retrieval/      SearXNG adapter, engine registry, merger, scoring   (Phase 1)
  extraction/     trafilatura, crawl4ai client, SSRF guard            (Phase 1)
  orchestration/  query understanding, planner, executor, synthesizer (Phase 1-3)
  llm/            provider-agnostic chat + embedding gateway          (Phase 1)
alembic/          schema migrations
tests/            unit tests (no live Postgres/Redis required)
```

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

`GET /healthz` is liveness, `GET /readyz` checks Postgres + Redis + SearXNG,
`GET /metrics` exposes Prometheus metrics and `GET /v1/meta` reports the
active giants configuration.

## Migrations

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe change"
```

`alembic/env.py` reads `DATABASE_URL` through `app.config.get_settings()`, so
the same `.env` used by the API drives migrations.
