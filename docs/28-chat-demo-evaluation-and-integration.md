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

## 6. 3 vòng audit đối kháng sau tích hợp (2026-11-16)

Ba expert read-only song song (logic/correctness · security/resilience ·
perf/integration), mỗi vòng trace bằng tay trên parser + đọc `node_modules`
để verify claim (không tin docs). Kết quả: **4 P1, ~14 P2, ~10 P3** — đã fix
toàn bộ P1/P2 và các P3 rẻ. Chi tiết:

**P1 đã fix:**
1. **Italic nuốt phép nhân** — `parseInline("2 * 3 * 4")` thành italic.
   Fix: left-flanking guard CommonMark (opener/closer không kề space).
2. **Tiền tệ có toán tử bị nuốt thành LaTeX** — `$revenue > $cost` thành
   math. Fix: chỉ marker MẠNH (`\cmd`, `^`, `_`) mới mở math; `=<>{}`
   không tự đủ; span kết-thúc-bằng-space bị từ chối.
3. **Throttle không gate phần đắt nhất** (3 vòng cùng chốt) — `parseBlocks`
   + dựng element chạy lại **mỗi token** (parent re-render per token),
   O(n²) qua stream. Fix: `useMemo` blocks trên `shown`, memoize render
   tree, `React.memo(AnswerMarkdown)` + `useMemo` citation map tại cả 4
   call-site (2 site trong JSX được bọc thành `ArtifactMarkdown` /
   `PostMarkdown` memoized).
4. **`gate.accept()` bị re-anchor bởi re-render không liên quan** —
   status event/keystroke đẩy cửa sổ pacing. Fix: chỉ accept khi snapshot
   thực sự thay đổi (`lastShownRef`).

**P2 đã fix:** cap `MAX_MATH_CHARS` cho đường `$$` single-line; scanner
`extractInlineMath` viết lại index-based O(n) + bail-out sau 64 opener fail
(chống `$`-dense DoS); lone-table rewind (dòng sau `| a |` không còn bị nuốt
— bug di truyền từ renderer cũ, nay heal); `MathFormula` last-good chỉ áp
cho cùng công thức (prefix check) + `maxSize: 20` chống layout DoS; block
key hybrid (stateful=content-hash chống remount cascade KaTeX/Mermaid,
text=occurrence); fence code cap 200k + truncation notice; table cap
500 hàng × 20 cột; fence mở (đang stream) render plain — Prism chỉ chạy khi
fence đóng; NaN-cost guard trong `gapForCost` (Infinity→cap, NaN→floor);
`safeHref` cap 2048; dead code `extractDisplayMath` được **wire vào**
paragraph path (hết drift parser↔renderer); depth cap `parseInline` thực
chất; empty code span literal; CRLF normalize 1 lần.

**P1 vận hành đã fix:** `frontend/out` (bản Lite serve tại `/`) **stale 2
ngày** — không gate nào chạy `build:export`, Lite sẽ serve renderer cũ.
Fix: `run_quality_gates` giờ chạy `build:export`; `run_pre_commit_gate`
giờ chạy thêm frontend vitest + typecheck (trước đó 0 check frontend ở
pre-commit); `out/` đã rebuild — verify `md-mermaid` có mặt trong chunks.

**Verify an toàn (closed, không fix cần):** Prism `Token.stringify` escape
toàn bộ — không sink HTML từ untrusted input; KaTeX `trust` default off →
`\href`/`\includegraphics` throw → caught; mermaid strict = DOMPurify 3.4.18
(scripts/handlers strip — comment "sandbox" sai đã sửa thành ghi chú đúng);
prismjs 1.30.0 đã fix CVE-2024-53382; `npm audit`: 0 vulns / 235 deps.
`npm ci` khớp lockfile. SSR/prerender: không window/document ở module/render
scope, initial state server/client identical.

**Cố ý giữ lại (có lý do):** inline `new Map` tại 2 call-site cold thay
bằng wrapper memoized; mermaid queued render không cancel khi unmount
(alive chỉ bỏ kết quả — cancel mid-flight không có API; tự-heal ≤2s);
`3*4*5` (không space) theo CommonMark = italic — chấp nhận spec.

