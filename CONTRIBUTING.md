# Contributing — MetaSearch

Nguyên tắc số 1: **giants-first** — đừng tự chế lại bánh xe. Đọc
[AGENTS.md](AGENTS.md) cho bức tranh tổng và [docs/](docs/) cho thiết kế.

## Setup dev (Windows / Git Bash)

```powershell
# API (Python 3.12)
cd api
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest

# Frontend (Node 22)
cd ../frontend
npm install
npm run dev        # http://localhost:3000

# Stack đầy đủ (cần Docker Desktop)
cd ../deploy
copy .env.example .env   # điền LLM_API_KEY, SEARXNG_SECRET
docker compose up -d
# API :8000 · UI :3000 · Swagger :8000/docs · Grafana :3001 (profile observability)
```

Không có Docker vẫn dev được: `uvicorn app.main:app --reload` — API tự degrade
(engine trả 503 có `engines_status`, DB/Redis fallback in-memory). Muốn search
thật mà không có Docker: cài SearXNG riêng và trỏ `SEARXNG_URL`.

## Workflow

1. **Branch** từ `main`, đặt tên `feat/...`, `fix/...`, `chore/...`.
2. **Implement** theo quy ước trong [AGENTS.md](AGENTS.md) §4 (graceful
   degradation, error model, deps DI, SSRF guard, bounded writes).
3. **Tests**: mỗi module mới có unit test; endpoint có test qua
   `TestClient` + fakes; hành vi degrade phải được test (xem
   `tests/test_api_v1.py`).
4. **Gate trước khi commit** (tất cả phải xanh):
   ```bash
   pytest -q
   ruff check app tests scripts && ruff format --check app tests scripts
   bandit -q -r app -x tests
   cd ../frontend && npm run typecheck && npm run build
   ```
5. **Commit message**: dạng `type(scope): summary` (xem `git log` làm mẫu).
6. **Docs đi cùng code**: checklist trong roadmap cần tick, CHANGELOG cần dòng
   mới nếu thay đổi đáng nhớ, AGENTS.md §2 cập nhật nếu trạng thái đổi.

## Thêm một search engine mới (target ≤ 1 file + 1 dòng)

1. Tạo `api/app/retrieval/adapters/<name>.py` — implement
   `SearchAdapter` protocol (tham khảo `tavily.py`: parse → `RawResult`,
   `AdapterError(retryable=...)`, không retry lỗi auth/429).
2. Đăng ký trong `engine_store.default_configs` (name/category/requires_key/
   adapter_path) + `registry.build_adapters` (chỉ enable khi đủ API key).
3. Thêm `tests/test_<name>.py` với `httpx.MockTransport` (success / auth-error /
   timeout-retry).
4. Không cần sửa pipeline — registry tự fan-out engine mới.

## Thêm một endpoint mới

1. Route file trong `api/app/api/v1/`, đăng ký ở `router.py`.
2. Request/response models trong `schemas.py`; lỗi qua `MetaSearchError`.
3. Dependencies resolve qua `deps.get_X()`; response theo spec `docs/06`
   (RFC 7807-ish error, SSE events chuẩn nếu stream).

## Bảo mật — kiểm tra bắt buộc khi đụng fetch/scrape

- URL user-supplied: `ssrf.resolve_and_check` + `pinning.build_pinned_request`,
  redirect re-check mỗi hop, cap `max_scrape_bytes`, robots.txt.
- Secrets chỉ qua env (`settings`), không log key, không commit `.env`.
- `bandit` phải 0 issue.
