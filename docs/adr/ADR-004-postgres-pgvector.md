# ADR-004 — Postgres + pgvector làm store duy nhất

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/05](../05-data-model.md), [docs/08 §2](../08-deployment.md)

## Context

Cần lưu documents/chunks/citations/answers (relational) + semantic cache L2 (vector). Trên máy 16GB không đủ chạy thêm vector DB riêng (Qdrant/Milvus ~500MB–1GB+ RAM).

## Decision

**Postgres 16 + pgvector** (HNSW index) làm store duy nhất — image `pgvector/pgvector:pg16`, `shared_buffers=256MB` trên dev. Embedding: prod 1024 dims (bge-m3 qua API); dev 16GB dùng e5-small int8 384 dims (~150MB) hoặc tắt L2 (mặc định).

## Rationale

- 1 DB cho relational + vector → ít service, backup 1 nơi (`pg_dump` nightly).
- HNSW đủ cho scale <10M chunks (mục tiêu v1).
- Citation validation cần transactional giữa documents/chunks/answers — cùng DB là tự nhiên.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| Qdrant/Milvus riêng | Thêm 1 service + RAM trên 16GB; lệch prod/dev |
| SQLite dev | Không có pgvector → lệch schema với prod |
| Elasticsearch | Nặng (2GB+ RAM), thừa cho v1 |

## Consequences

- pgvector chậm hơn dedicated vector DB ở scale rất lớn — không phải mục tiêu v1; scaling path có read replica + partition ([docs/08 §5](../08-deployment.md)).
- Đổi embedding model → phải reindex; ghi `embedding_model` vào metadata chunks.
