# docs/28 — Đánh giá & tích hợp chọn lọc deepseek-ai/chat-demo (v3.2.1)

**Ngày:** 2026-11-16 · **Protocol:** Principal Full-Stack Architect — Phase 1
(evaluation, mandatory checkpoint) → Phase 2 (selective integration).
**Phạm vi duyệt cuối:** 4 mục `SUPERIOR` (throttle, code fences + inline md,
KaTeX, Mermaid) — owner xác nhận tại checkpoint.

## 1. Đối tượng đánh giá

[deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo) (MIT): chat
client tĩnh **frontend-only** cho DeepSeek API — ~19,200 LOC (14,517 TS/TSX +
~4,650 CSS), React 18.3.1 + zustand 4.4.7 + rspack, **không** dùng UI lib
(hand-rolled components). Browser gọi thẳng DeepSeek API; dữ liệu client-side
(IndexedDB). Audit nguồn thực hiện trên clone đầy đủ `src/` (117 file) —
không tin README.

## 2. Comparison & Decision Matrix (điểm chính)

| # | Feature/Mechanism | MetaSearch hiện có | chat-demo | Verdict | Effort/Risk |
|---|---|---|---|---|---|
| 1 | SSE parsing | `lib/api.ts` `streamSse`: CRLF cross-chunk (audit r12), cap 1MB buffer + 1MB/event, abort signal, tested | WHATWG-spec decoder tự viết | **PARITY (Skip)** — ta có buffer cap họ không có | — |
| 2 | Token-by-token smoothness | Re-parse toàn bộ text mỗi SSE delta → O(n²) khi stream report dài | Adaptive render-cost throttle (43 LOC, zero-dep) | **SUPERIOR (Integrate)** | Low/Low |
| 3 | Fenced code blocks | **BROKEN** — fence render thành paragraphs | CodeBlock + copy + Prism lazy per-language (dep-ordering) | **SUPERIOR (Integrate)** — fix bug + nâng | Low/Low |
| 4 | KaTeX math | Không có — LaTeX hiện raw text | katex lazy + keep-last-successful-render khi stream dở | **SUPERIOR (Integrate)** | Medium/Low |
| 5 | Mermaid diagrams | Không có | Serial render queue + `securityLevel:'strict'` + streaming fallback | **SUPERIOR (Integrate)** | Medium/Low |
| 6 | Inline md (code/italic/quote) | Thiếu inline code, italic, blockquote | Full qua mdast | **SUPERIOR (Integrate)** (gộp #3) | Low/Low |
| 7 | Citations | `[n]` clickable + sources panel | SourceRegistry + hover card | **PARITY (Skip)** | — |
| 8 | Message tree/branching/IndexedDB | Không có (search UI, persist server-side) | Full branching + idb | **REJECT** — khác sản phẩm | — |
| 9 | History ZIP export | CSV export FA, dữ liệu server-side | ZIP32 reader/writer tự viết | **REJECT** | — |
| 10 | DeepThink/Search toggles | `deep:true` + 19 engines | DeepSeek API params | **REJECT** — API-specific | — |
| 11 | Attachments/Files API | Backend không có upload API | Files API + self-heal retry | **REJECT** — cần contract mới | — |
| 12 | Speech | Web Speech vi-VN + VoiceStudio (34 tests) | Server ASR/TTS + PCM player + spectrum | **PARITY (Skip)** — PCM player chỉ đáng khi VoiceStudio stream PCM | — |
| 13 | SSE spec decoder thay thế | streamSse hardened | sse.ts 136 LOC | **PARITY (Skip)** | — |
| 14 | Light theme/i18n/mobile hacks | Dark-only VI, responsive grid | CSS tokens + i18n + visualViewport | **REJECT** — design choice | — |

**Kết luận:** 10/10 "novel mechanisms" của họ nằm ở REJECT/PARITY; giá trị
thật cô đọng ở tầng **streaming rich-content rendering** — đúng điểm yếu nhất
của renderer line-based hiện tại.

## 3. Đã tích hợp (Phase 2 — cherry-pick, không overwrite)

Quy tắc tuân thủ: giữ nguyên kiến trúc Next.js/React 19, giữ nguyên contract
`AnswerMarkdown({ text, citationByUrl })` (3 call-sites không đổi), giữ nguyên
citation contract, không đụng backend/auth. **Deps mới (đã justify):**
`katex` (giant duy nhất render LaTeX), `mermaid` (~1MB, lazy — chỉ load khi
gặp fence `mermaid`), `prismjs` (lazy per-language). Tất cả **dynamic import**
→ First Load JS 135 kB (không đổi đáng kể), không có gì vào main bundle.

| File mới | Vai trò | Port từ |
|---|---|---|
| `frontend/lib/mdMath.ts` | Tách math `$...$`/`$$..$$`/`\(...\)` với **guard chống nhầm tiền tệ** (`isMathish`/`hasMathMarkers` — "$1,234" không bị nuốt thành LaTeX), cap 2000 chars, merge segments | Pattern `micromark-extension-llm-math` + guard tự thiết kế cho FA content |
| `frontend/lib/renderThrottle.ts` | `AdaptiveRenderGate` thuần (clock-injectable): accept snapshot sau `cost×2` (floor 16ms, cap 2s) | `useThrottleMarkdownRender` |
| `frontend/lib/inlineMd.ts` | Parser inline typed segments: code/bold/italic/cite/math, scanner recursive + collapse tolerance | Pattern components/markdown |
| `frontend/lib/markdownBlocks.ts` | `parseBlocks` typed blocks: fence (closed/unclosed), `$$` display math (guarded), quote, table, heading, list-item | Pattern services/markdown |
| `frontend/lib/codeHighlight.ts` | Prism lazy per-language với **explicit loaders map** (tránh context-bundle 597 files), dep-ordering (cpp←c, tsx←jsx+ts), alias map, SSR-guard fail-open | `services/markdown/highlight.ts` |
| `frontend/components/CodeBlock.tsx` | Fence container + copy button + highlight fail-open | `CodeBlock.tsx` |
| `frontend/components/MathFormula.tsx` | KaTeX `renderToString` + **keep-last-successful-render** khi stream LaTeX dở; fallback raw | `MathFormula.tsx` |
| `frontend/components/MermaidDiagram.tsx` | **Serial render queue** (module-level chain), strict sandbox, cap 20k chars, fallback pre/code | `MermaidDiagram.tsx` + `mermaid.ts` |
| `frontend/types/prismjs-components.d.ts` | Wildcard declaration cho component subpaths (side-effect contract) | — |

**Sửa:** `frontend/components/AnswerMarkdown.tsx` (map qua typed blocks +
throttle hook dùng chung 1 gate với measurement), `frontend/app/globals.css`
(styles cho `.md-code`, `.md-quote`, `.math-*`, `.md-mermaid`).

**Sai lệch có chủ ý so với chat-demo:**
- Throttle đo parse-cost trong render body + shared gate (bản họ đo trong
  component body riêng) — 1 gate duy nhất tránh lệch state.
- Streaming-fallback của Mermaid: không retry-timer 100ms; chỉ render diagram
  khi fence **đã đóng** — fence dở render là CodeBlock (đơn giản hơn, đúng).
- Inline `$...$` yêu cầu marker LaTeX (`\^_{}=<>`) — bắt buộc cho nội dung
  tài chính VN ("$1,234" là tiền, không phải math).
- Không port mhchem (chemistry) — chưa có use case, tránh phình CSS/JS.

## 4. Verification

**Automated (đã chạy, xanh):**
- `npm test` — **89/89** (79 cũ + 10 mới: mdMath 12, inlineMd 8, markdownBlocks 11,
  renderThrottle 7, codeHighlight 2) — bao gồm edge cases: tiền tệ, escaped `\$`,
  `\(...\)` close-delimiter, fence unclosed mid-stream, `$$` unterminated,
  math không marker, SSR fail-open, gate pacing up/down.
- `npm run typecheck` — sạch.
- `npm run build` — xanh; First Load JS **135 kB** (deps mới không vào main).

**Checklist thủ công (khi chạy `npm run dev` + API):**
1. Stream câu trả lời dài (deep research) — repaint ≤ 2s/lần khi report lớn,
   không lag gõ phím; nội dung cuối cùng luôn hiện đầy đủ sau stream xong.
2. Câu hỏi tài chính trả về "$1,234" / "US$500" — hiển thị đúng, không bị
   render thành LaTeX.
3. Answer chứa ```python fence — code block có banner ngôn ngữ + nút Copy
   hoạt động; copy không mang trailing newline thừa.
4. Answer chứa ```mermaid fence đóng — diagram vẽ ra; fence dở (đang stream)
   — hiện CodeBlock, không crash.
5. LaTeX `$\frac{LN}{EQ}$` inline và `$$...$$` block — render công thức;
   LaTeX dở giữa stream — giữ công thức hoàn chỉnh gần nhất, không nháy lỗi.
6. Citation `[n]` — vẫn clickable, mở đúng `safeHref`, sources panel không đổi.
7. Tab Tin tức / FA / Chart — không đổi (không đụng).
8. Mất mạng giữa stream — SSE client hiện có báo lỗi như trước (không đổi).

## 5. Rủi ro còn mở

- `mermaid` lazy chunk ~1MB: chỉ tải khi diagram xuất hiện lần đầu — chấp
  nhận được cho UI nội bộ; nếu cần có thể chuyển render server-side sau.
- Prism highlight trong luồng stream: `CodeBlock` re-highlight mỗi snapshot —
  bounded bởi throttle gate; code fence dài (>100k chars) skip highlight.
- KaTeX CSS (+~24KB) vào CSS bundle của page có `MathFormula` — hợp lý vì
  math xuất hiện ngay trong answer stream.
