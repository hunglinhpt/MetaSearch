# ADR-003 — Extraction hybrid 3 tầng

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/03 stage 8–9](../03-search-pipeline.md), [docs/02 §2](../02-system-architecture.md)

## Context

Sau meta-search cần đọc nội dung top-k URL. Đa số trang là tĩnh (trafilatura đủ), một phần là JS-heavy (báo VN, SPA — cần browser). Chạy Playwright in-process trong api tốn RAM (16GB không đủ) và khởi động chậm.

## Decision

Fallback chain 3 tầng, dừng ở tầng đầu thành công:

1. **trafilatura in-process** (~50–200ms, ~0 RAM thêm) — fetch tĩnh + rút gọn Markdown.
2. **crawl4ai service** (Docker, `POST /md`) — Playwright/Chromium đóng gói sẵn trong image, concurrency 2 trên 16GB.
3. **jina reader** (`r.jina.ai`) — escape hatch cho trang khó, không tự vận hành.

Heuristic phát hiện JS-heavy: sau fetch tĩnh, nếu không đủ ký tự `<p>`/nội dung chính → chuyển tầng 2.

## Rationale

- 80–90% trang tĩnh xử lý ở tầng rẻ nhất; browser chỉ sống khi cần → RAM stack ≤4GB.
- Không tự cài Chromium trong api; crawl4ai là giant #2, nâng image là xong.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| crawl4ai cho mọi URL | Chậm + RAM cao cho trang tĩnh; bỏ qua 50–200ms của trafilatura |
| Chỉ trafilatura | Rớt toàn bộ trang JS-heavy |
| jina reader làm primary | Phụ thuộc bên thứ 3 + rate limit; chỉ dùng làm tầng cuối |

## Consequences

- 3 code path cần test ([docs/11](../11-testing-strategy.md) golden set gồm cả trang JS).
- SSRF guard áp dụng trước tầng 1 (scheme allowlist, block private nets, pin-IP, max 3 redirects, 5MB, 8s).
