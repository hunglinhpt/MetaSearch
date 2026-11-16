"""Automated Scanning Scheduler & Daily Briefing / Blog Synthesizer.

Provides automated periodic and on-demand scans for key domains
(docs/29 P0-1): AI Coding, AI Agents, Chứng khoán VN, Kinh tế vĩ mô,
Phong thủy, Sức khỏe — plus one **unified run digest** ("Bản tin chung")
that aggregates the whole scan run.

docs/29 integrity rules for every synthesized post:
- content is EXTRACTIVE — every factual line comes from real search results
  (title + snippet); no hardcoded market/analysis narratives;
- citations enumerate deduped REAL urls only (canonicalize + simhash fold),
  never a placeholder url;
- untrusted titles/snippets are markdown-escaped before embedding;
- the delta section never asserts continuity it cannot prove.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import BlogPost
from app.db.session import get_sessionmaker
from app.orchestration.notifier import NotificationDispatcher
from app.retrieval.canonical import canonicalize
from app.retrieval.registry import EngineRegistry
from app.retrieval.simhash import is_near_duplicate, simhash64

logger = get_logger(__name__)

RUN_DIGEST_TOPIC = "run_digest"

# Detached notifier tasks — kept referenced so GC cannot cancel mid-flight.
_background_notify_tasks: set[asyncio.Task[None]] = set()

# Per-topic wall-clock cap (engines ≤5s + DB + dedup; notifier is detached).
_SCAN_TOPIC_TIMEOUT_S = 20.0
# Parallel topics at once — keeps DDG politeness and shared pools healthy.
_SCAN_CONCURRENCY = 2
_MAX_CITATIONS_PER_TOPIC = 10
_MAX_CITATIONS_PER_RUN = 30


@dataclass(slots=True)
class ScanTopic:
    id: str
    name: str
    query: str
    category: str
    tags: list[str]
    description: str
    last_run: datetime | None = None
    last_post_id: str | None = None


DEFAULT_TOPICS: dict[str, ScanTopic] = {
    "ai_coding": ScanTopic(
        id="ai_coding",
        name="AI Coding & LLM Development",
        query="AI coding models Claude Sonnet Cursor IDE agent code generation benchmark",
        category="developer",
        tags=["ai-coding", "llm", "developer-tools", "cursor", "sonnet"],
        description=(
            "Tổng hợp xu hướng AI hỗ trợ lập trình, mô hình code thế hệ mới, "
            "agentic coding IDE và benchmark hiệu năng kỹ thuật."
        ),
    ),
    "ai_agents": ScanTopic(
        id="ai_agents",
        name="AI Agents & Autonomous Systems",
        query="AI agents autonomous workflows MCP protocol multi agent tool calling deep research",
        category="developer",
        tags=["ai-agents", "autonomous-systems", "mcp", "deep-research", "multi-agent"],
        description=(
            "Bản tin chuyên sâu về kiến trúc Agent tự trị, chuẩn giao tiếp Model Context Protocol (MCP), "
            "hệ thống multi-agent và cơ chế deep research."
        ),
    ),
    "viet_finance": ScanTopic(
        id="viet_finance",
        name="Thị Trường Chứng Khoán & Tài Chính Việt Nam",
        query="thị trường chứng khoán việt nam VN-Index cổ phiếu thanh khoản lãi suất vĩ mô FPT HPG VCB",
        category="financial",
        tags=["chung-khoan-vn", "vn-index", "tai-chinh", "co-phieu", "vi-mo"],
        description=(
            "Phân tích chuyên sâu diễn biến chỉ số VN-Index, thanh khoản thị trường, định giá P/E, "
            "biến động các mã cổ phiếu dẫn dắt (FPT, HPG, VCB,...) và chính sách tiền tệ."
        ),
    ),
    "macro_business": ScanTopic(
        id="macro_business",
        name="Kinh Doanh & Kinh Tế Vĩ Mô",
        query="kinh tế vĩ mô kinh doanh đầu tư lạm phát lãi suất fed startup công nghệ",
        category="news",
        tags=["kinh-doanh", "vi-mo", "lai-suat", "dau-tu", "startup"],
        description=(
            "Nhận định vĩ mô kinh tế, xu hướng lãi suất, chuỗi cung ứng toàn cầu "
            "và các thương vụ đầu tư công nghệ nổi bật."
        ),
    ),
    "fengshui_lifestyle": ScanTopic(
        id="fengshui_lifestyle",
        name="Phong Thủy Ứng Dụng & Không Gian Sống",
        query="phong thủy ứng dụng tài lộc bố trí bàn làm việc văn phòng nhà ở bát trạch ngũ hành",
        category="fengshui",
        tags=["phong-thuy", "bat-trach", "ngu-hanh", "khong-gian-song", "tai-loc"],
        description=(
            "Ứng dụng nguyên lý Bát Trạch, Can Chi, Ngũ Hành nạp âm vào kiến trúc, "
            "bài trí bàn làm việc tăng vượng khí và cân bằng năng lượng sinh hoạt."
        ),
    ),
    "health_wellness": ScanTopic(
        id="health_wellness",
        name="Y Học & Sức Khỏe Đời Sống",
        query="sức khỏe dinh dưỡng y học tiến bộ nghiên cứu lối sống khoa học phòng ngừa bệnh lý",
        category="academic",
        tags=["suc-khoe", "y-hoc", "dinh-duong", "loi-song-khoa-hoc", "phong-ngua"],
        description=(
            "Cập nhật các tiến bộ y sinh học, bằng chứng lâm sàng về dinh dưỡng, "
            "chăm sóc sức khỏe chủ động và cải thiện chất lượng sống."
        ),
    ),
}


def _md_escape(text: str, limit: int = 220) -> str:
    """Flatten and neutralize markdown-structural chars in untrusted text
    (AppSec F7): a title containing `](` must not hijack citation links."""
    cleaned = text.replace("\r", " ").replace("\n", " ")
    for ch in ("[", "]", "(", ")", "*", "_", "`", "<", ">", "#"):
        cleaned = cleaned.replace(ch, " ")
    return re.sub(r"\s+", " ", cleaned).strip()[:limit]


def _dedup_results(results: list[Any]) -> list[dict[str, Any]]:
    """Canonical-URL dedup + simhash fold over raw adapter results.

    Returns dicts with real fields only (no fabricated url): title, url,
    snippet, engines (engine ids that surfaced it), hash. O(n²) on n≤15.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in results:
        url = (getattr(r, "url", "") or "").strip()
        if not url:
            continue  # a result without a url is never turned into a source
        try:
            key = canonicalize(url)
        except Exception:
            key = url
        title = _md_escape(getattr(r, "title", ""), 200)
        snippet = _md_escape(getattr(r, "snippet", ""), 400)
        engine = getattr(r, "adapter", "") or ""
        if key in groups:
            g = groups[key]
            if engine and engine not in g["engines"]:
                g["engines"].append(engine)
            if len(g["snippet"]) < len(snippet):
                g["snippet"] = snippet  # keep the richest snippet
            continue
        entry: dict[str, Any] = {
            "title": title or url,
            "url": url,
            "snippet": snippet,
            "engines": [engine] if engine else [],
            "hash": simhash64(f"{title} {snippet}"),
        }
        groups[key] = entry
        order.append(key)

    # Near-dup fold across different URLs (same story, syndicated).
    out: list[dict[str, Any]] = []
    for key in order:
        entry = groups[key]
        merged = False
        for kept in out:
            if is_near_duplicate(entry["hash"], kept["hash"]):
                if len(entry["snippet"]) > len(kept["snippet"]):
                    kept["snippet"] = entry["snippet"]
                for e in entry["engines"]:
                    if e not in kept["engines"]:
                        kept["engines"].append(e)
                merged = True
                break
        if not merged:
            out.append(entry)
    return out


def _build_delta_section(
    previous_post: BlogPost | None,
    deduped: list[dict[str, Any]],
) -> str:
    """Honest delta vs the previous digest — canonical URL set comparison
    over the deduped entries; never asserts continuity it cannot prove
    (docs/29 P0-1). Lists up to 3 fresh titles (escaped)."""
    prev_published = getattr(previous_post, "published_at", None) if previous_post else None
    prev_date = prev_published.strftime("%d/%m/%Y") if prev_published else "kỳ trước"
    if not previous_post:
        return (
            "## ⚡ Bối Cảnh Khởi Điểm\n"
            "Đây là bản tin mở đầu cho chuỗi theo dõi tự động của chủ đề này. "
            "Toàn bộ nguồn tin trong kỳ này đóng vai trò mốc tham chiếu cơ sở "
            "cho các kỳ đối chiếu tiếp theo.\n"
        )

    prev_urls: set[str] = set()
    for s in getattr(previous_post, "sources", []) or []:
        if isinstance(s, dict) and s.get("url"):
            try:
                prev_urls.add(canonicalize(str(s["url"])))
            except Exception:
                prev_urls.add(str(s["url"]))

    def _canon(url: str) -> str:
        try:
            return canonicalize(url)
        except Exception:
            return url

    # PASS 2 P1-3: compare CANONICAL forms — raw urls with www./tracking
    # params must not be reported "fresh" every run.
    fresh = [e for e in deduped if _canon(e["url"]) not in prev_urls]
    if fresh:
        bullets = "\n".join(f"- 🆕 **{e['title']}** — {e['snippet'][:140]}" for e in fresh[:3])
        return (
            f"## ⚡ Nguồn Mới So Với Kỳ {prev_date} ({len(fresh)} nguồn)\n"
            f"{bullets}\nDanh sách nguồn đầy đủ ở phần tham khảo.\n"
        )
    return (
        f"## ⚡ So Với Kỳ {prev_date}\n"
        f"Không ghi nhận nguồn mới nào — toàn bộ nguồn trong kỳ này đã xuất "
        f"hiện ở kỳ trước.\n"
    )


def _safe_link_url(url: str) -> str:
    """Percent-encode characters that would terminate a markdown link early
    (PASS 2 P3-12: Wikipedia-style parens like .../Python_(language))."""
    from urllib.parse import quote

    return quote(url, safe=":/?#[]@!$&'*+,;=%~")


def _sources_block(citations: list[dict[str, Any]]) -> str:
    if not citations:
        return "- Không thu được nguồn tin hợp lệ trong kỳ này."
    return "\n".join(
        f"- [{c['n']}] [{c['title']}]({_safe_link_url(c['url'])}) — {c['snippet'][:160]}"
        + ("…" if len(c["snippet"]) > 160 else "")
        + (f" *(engine: {', '.join(c['engines'])})*" if c["engines"] else "")
        for c in citations
    )


def synthesize_blog_content(
    topic: ScanTopic,
    results: list[Any],
    now: datetime,
    previous_post: BlogPost | None = None,
    engines_ok: int = 0,
    engines_failed: int = 0,
) -> tuple[str, str, str, str, list[dict[str, Any]]]:
    """Extractive per-topic briefing (docs/29 P0-1): every factual line is a
    real result; headers only frame the topic, they never assert facts."""
    date_str = now.strftime("%d/%m/%Y")
    slug_date = now.strftime("%Y-%m-%d")
    slug = f"{topic.id}-{slug_date}-{str(uuid.uuid4())[:8]}"

    deduped = _dedup_results(results)
    citations: list[dict[str, Any]] = []
    for idx, entry in enumerate(deduped[:_MAX_CITATIONS_PER_TOPIC], start=1):
        citations.append(
            {
                "n": idx,
                "title": entry["title"],
                "url": entry["url"],
                "snippet": entry["snippet"],
                "engines": entry["engines"],
            }
        )
    delta_section = _build_delta_section(previous_post, deduped)

    engine_line = (
        f"Số liệu thu thập: {len(deduped)} tin (sau khử trùng lặp) từ "
        f"{engines_ok} engine thành công"
        + (f", {engines_failed} engine lỗi." if engines_failed else ".")
        if deduped
        else f"Không thu được tin nào từ {engines_ok} engine đã hỏi."
    )

    title = f"{topic.name} — Bản Tin Ngày {date_str}"
    summary = (
        f"Tổng hợp nguồn tin thực tế ngày {date_str} cho chủ đề {topic.name}: "
        f"{len(deduped)} tin sau khử trùng lặp, trích dẫn đầy đủ."
    )

    if deduped:
        top_block = "\n".join(
            f"{idx}. **{c['title']}** — {c['snippet'][:200]}"
            + ("…" if len(c["snippet"]) > 200 else "")
            + (f" *(engine: {', '.join(c['engines'])})*" if c["engines"] else "")
            + f" [Nguồn {c['n']}]"
            for idx, c in enumerate(citations[:5], start=1)
        )
        detail_block = "\n".join(
            f"- **{c['title']}** [Nguồn {c['n']}]: {c['snippet'][:300]}"
            + ("…" if len(c["snippet"]) > 300 else "")
            for c in citations[:3]
        )
    else:
        top_block = "Không có tin hợp lệ trong kỳ này — không suy diễn, không ước lượng."
        detail_block = "_Chưa có dữ liệu thực để trích._"

    content_md = f"""# {title}

*Bản tin extractive do MetaSearch Scanning Engine tổng hợp ngày {date_str} — toàn bộ nội dung dưới đây trích từ nguồn tin thực tế, không nhận định thay nguồn.*

**{engine_line}**
Phạm vi chủ đề: {topic.description}

## Tin Nổi Bật Trong Kỳ
{top_block}

## Trích Dẫn Chi Tiết (Top {min(3, len(citations))})
{detail_block}

{delta_section}
---
### Nguồn Dữ Liệu Tham Khảo
{_sources_block(citations)}
"""
    return slug, title, summary, content_md, citations


@dataclass(slots=True)
class _ScanOutcome:
    """Per-topic outcome feeding the unified run digest."""

    topic: ScanTopic
    post: BlogPost | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    raw_count: int = 0
    deduped_count: int = 0
    engines_ok: int = 0
    engines_failed: int = 0
    error: str | None = None


def synthesize_run_digest(
    outcomes: list[_ScanOutcome],
    now: datetime,
    previous_digest: BlogPost | None = None,
) -> tuple[str, str, str, str, list[dict[str, Any]]]:
    """The single unified digest for the whole scan run ("Bản tin chung").

    Aggregates every topic outcome into one post: run statistics, per-topic
    highlights (real titles only), cross-topic hot sources, and one merged
    citation list with global numbering.
    """
    date_str = now.strftime("%d/%m/%Y")
    slug_date = now.strftime("%Y-%m-%d")
    slug = f"run-digest-{slug_date}-{str(uuid.uuid4())[:8]}"

    raw_total = sum(o.raw_count for o in outcomes)
    deduped_total = sum(o.deduped_count for o in outcomes)
    engines_ok = sum(o.engines_ok for o in outcomes)
    engines_failed = sum(o.engines_failed for o in outcomes)

    # Global citation numbering over ALL topics (url-deduped across topics).
    global_sources: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for o in outcomes:
        for c in o.citations:
            try:
                key = canonicalize(c["url"])
            except Exception:
                key = c["url"]
            if key in global_sources:
                global_sources[key]["topics"].add(o.topic.name)
                continue
            global_sources[key] = {**c, "topics": {o.topic.name}}
            order.append(key)
    citations: list[dict[str, Any]] = []
    for idx, key in enumerate(order[:_MAX_CITATIONS_PER_RUN], start=1):
        g = global_sources[key]
        citations.append(
            {
                "n": idx,
                "title": g["title"],
                "url": g["url"],
                "snippet": g["snippet"],
                "engines": g["engines"],
                "topics": sorted(g["topics"]),
            }
        )

    def _canon_url(url: str) -> str:
        try:
            return canonicalize(url)
        except Exception:
            return url

    # PASS 2 P2-4: key by canonical form — two topics citing the same story
    # with different raw url forms (www/http/tracking) must resolve to the
    # same merged global citation.
    by_url = {_canon_url(c["url"]): c for c in citations}

    # Cross-topic hot sources — the same url surfaced by ≥2 topics.
    hot = [c for c in citations if len(c["topics"]) >= 2]
    hot_block = (
        "\n".join(
            f"- **{c['title']}** [Nguồn {c['n']}] — xuất hiện ở {', '.join(c['topics'])}"
            for c in hot[:5]
        )
        if hot
        else "Không có nguồn nào xuất hiện ở nhiều chủ đề trong đợt này."
    )

    # Whole-run top-5: prefer sources reported by multiple engines/topics.
    ranked = sorted(
        citations,
        key=lambda c: (len(c["topics"]), len(c["engines"])),
        reverse=True,
    )[:5]
    top_block = (
        "\n".join(
            f"{idx}. **{c['title']}** [Nguồn {c['n']}] — {c['snippet'][:180]}"
            + ("…" if len(c["snippet"]) > 180 else "")
            + (f" *(chủ đề: {', '.join(c['topics'])})*" if c["topics"] else "")
            for idx, c in enumerate(ranked, start=1)
        )
        or "Không có tin hợp lệ trong đợt chạy này."
    )

    topic_sections: list[str] = []
    for o in outcomes:
        if o.post is None:
            topic_sections.append(
                f"### {o.topic.name}\nLỗi khi quét chủ đề: {o.error or 'không xác định'}."
            )
            continue
        cits = o.citations
        lead_cit = next((c for c in cits if _canon_url(c["url"]) in by_url), None)
        head = f"### {o.topic.name} ({len(cits)} tin sau khử trùng lặp)"
        if lead_cit is not None:
            g = by_url[_canon_url(lead_cit["url"])]
            topic_sections.append(f"{head}\nĐại diện: **{lead_cit['title']}** [Nguồn {g['n']}]")
        elif cits:
            # global citation list truncated at 30 — still truthful
            topic_sections.append(
                f"{head}\nĐại diện: **{cits[0]['title']}** (nguồn đầy đủ trong bài chủ đề)"
            )
        else:
            topic_sections.append(f"{head}\nKhông có tin hợp lệ trong kỳ này.")

    stats_line = (
        f"**Thống kê đợt chạy ({now.strftime('%Y-%m-%d %H:%M UTC')}):** "
        f"{len(outcomes)} chủ đề · {raw_total} tin thô → {deduped_total} sau khử trùng lặp · "
        f"{engines_ok} engine thành công / {engines_failed} lỗi."
    )
    delta_entries = [
        {"title": g["title"], "url": g["url"], "snippet": g["snippet"], "engines": g["engines"]}
        for g in (global_sources[k] for k in order[:_MAX_CITATIONS_PER_RUN])
    ]
    delta_section = _build_delta_section(previous_digest, delta_entries)

    title = f"Bản Tin Chung Toàn Đợt — {date_str}"
    summary = (
        f"Bản tin chung ngày {date_str}: {deduped_total} tin sau khử trùng "
        f"lặp từ {len(outcomes)} chủ đề, citation hợp nhất toàn đợt."
    )
    content_md = f"""# {title}

*Bản tin CHUNG cho toàn bộ đợt quét — nắm trọn mọi chủ đề, mọi nguồn trong một trang. Nội dung extractive từ nguồn thật.*

{stats_line}

## Tin Nổi Bật Toàn Đợt (ưu tiên nguồn đa chủ đề / đa engine)
{top_block}

## Toàn Cảnh Theo Chủ Đề
{chr(10).join(topic_sections)}

## Nguồn Nổi Bật Liên Chủ Đề
{hot_block}

{delta_section}
---
### Nguồn Dữ Liệu Tham Khảo (Hợp Nhất Toàn Đợt)
{_sources_block(citations)}
"""
    return slug, title, summary, content_md, citations


class AutomatedScanner:
    """Manages scheduled periodic scans and on-demand blog digest creation."""

    def __init__(
        self,
        registry: EngineRegistry,
        notifier: NotificationDispatcher | None = None,
    ) -> None:
        self.registry = registry
        self.topics = dict(DEFAULT_TOPICS)
        self.notifier = notifier or NotificationDispatcher()
        self._background_task: asyncio.Task[None] | None = None
        self._running = False
        self._memory_posts: deque[BlogPost] = deque(maxlen=200)
        self._scan_lock = asyncio.Lock()

    def list_memory_posts(
        self, *, topic: str | None = None, limit: int = 10, offset: int = 0
    ) -> list[BlogPost]:
        items = [p for p in self._memory_posts if topic is None or p.topic == topic]
        return items[offset : offset + limit]

    def get_memory_post(self, slug_or_id: str) -> BlogPost | None:
        for p in self._memory_posts:
            if p.slug == slug_or_id or str(p.id) == slug_or_id:
                return p
        return None

    async def _fetch_previous_post(self, topic_id: str) -> BlogPost | None:
        try:
            async with get_sessionmaker()() as session:
                stmt = (
                    select(BlogPost)
                    .where(BlogPost.topic == topic_id)
                    .order_by(BlogPost.published_at.desc())
                    .limit(1)
                )
                res = await session.execute(stmt)
                return res.scalars().first()
        except Exception as exc:
            logger.warning("scanner_fetch_prev_post_failed", topic=topic_id, error=str(exc))
            mem = self.list_memory_posts(topic=topic_id, limit=1)
            return mem[0] if mem else None

    async def _scan_topic_detailed(self, topic_id: str) -> _ScanOutcome:
        topic = self.topics.get(topic_id)
        if not topic:
            raise ValueError(f"Unknown topic: {topic_id}")
        outcome = _ScanOutcome(topic=topic)

        now = datetime.now(UTC)
        logger.info("scanner_scan_topic_start", topic=topic_id, query=topic.query)
        previous_post = await self._fetch_previous_post(topic_id)

        enabled_engines = await self.registry.enabled()
        matching_adapters = [
            (cfg, adapter)
            for cfg, adapter in enabled_engines
            if cfg.category in (topic.category, "news", "general")
        ]
        if not matching_adapters:
            matching_adapters = enabled_engines[:3]

        async def _query_engine(c_cfg, c_adapter):
            try:
                return await c_adapter.search(
                    topic.query,
                    max_results=5,
                    timeout_s=c_cfg.timeout_ms / 1000,
                )
            except Exception as exc:
                logger.warning(
                    "scanner_adapter_failed",
                    topic=topic_id,
                    engine=c_cfg.id,
                    error=str(exc),
                )
                return None  # sentinel: engine failed (kept out of results)

        tasks = [_query_engine(cfg, adapter) for cfg, adapter in matching_adapters[:3]]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        collected_results: list[Any] = []
        for res in gathered:
            if isinstance(res, list):
                collected_results.extend(res)
        outcome.engines_ok = sum(1 for r in gathered if isinstance(r, list))
        outcome.engines_failed = len(gathered) - outcome.engines_ok
        outcome.raw_count = len(collected_results)

        deduped = _dedup_results(collected_results)
        outcome.deduped_count = len(deduped)
        slug, title, summary, content_md, citations_list = synthesize_blog_content(
            topic,
            collected_results,
            now,
            previous_post=previous_post,
            engines_ok=outcome.engines_ok,
            engines_failed=outcome.engines_failed,
        )
        outcome.citations = citations_list

        post_id = uuid.uuid4()
        post = BlogPost(
            id=post_id,
            slug=slug,
            topic=topic.id,
            title=title,
            summary=summary,
            content_md=content_md,
            tags=topic.tags,
            sources=citations_list,
            published_at=now,
            created_at=now,
        )
        # PASS 2 P2-6: publish the outcome BEFORE any await — a 20 s timeout
        # landing mid-persist must not leave a "ghost" post that is written
        # but reported failed (and rescanned next run).
        outcome.post = post
        self._memory_posts.appendleft(post)

        # Best-effort persist into BlogPost table
        try:
            async with get_sessionmaker()() as session:
                session.add(post)
                await session.commit()
        except Exception as exc:
            logger.warning("scanner_persist_db_failed", topic=topic_id, error=type(exc).__name__)

        topic.last_run = now
        topic.last_post_id = str(post_id)
        logger.info("scanner_scan_topic_complete", topic=topic_id, post_id=str(post_id))
        return outcome

    async def scan_topic(self, topic_id: str) -> BlogPost:
        outcome = await self._scan_topic_detailed(topic_id)
        if outcome.post is None:
            raise RuntimeError(f"scan_topic failed for {topic_id}")
        # Notifications detached — must never block or fail post creation.
        try:
            await self.notifier.send_briefing_notification(outcome.post)
        except Exception as exc:
            logger.warning("scanner_notify_failed", post_id=str(outcome.post.id), error=str(exc))
        return outcome.post

    async def _persist_digest_only(self, post: BlogPost) -> None:
        """Persist-only path — notifications are spawned detached so the
        single-flight lock is never held behind channel HTTP (PASS 2 P3-11)."""
        try:
            async with get_sessionmaker()() as session:
                session.add(post)
                await session.commit()
        except Exception as exc:
            logger.warning("scanner_persist_db_failed", topic=post.topic, error=type(exc).__name__)

    def _spawn_notify(self, post: BlogPost) -> None:
        async def _notify() -> None:
            try:
                await self.notifier.send_briefing_notification(post)
            except Exception as exc:
                logger.warning("scanner_notify_failed", post_id=str(post.id), error=str(exc))

        task = asyncio.create_task(_notify())
        _background_notify_tasks.add(task)
        task.add_done_callback(_background_notify_tasks.discard)

    async def scan_all(
        self, *, respect_last_run: bool = False, interval_hours: float = 24.0
    ) -> list[BlogPost]:
        """Scan all topics in parallel (bounded), then publish ONE unified
        run digest aggregating the whole run (docs/29 P0-1)."""
        async with self._scan_lock:  # single-flight: no double-scan bursts
            now = datetime.now(UTC)
            horizon = timedelta(hours=interval_hours * 0.9)
            topics = [
                t
                for t in self.topics.values()
                if not (respect_last_run and t.last_run and now - t.last_run < horizon)
            ]
            if not topics:
                logger.info("scanner_scan_all_skipped_fresh", interval_hours=interval_hours)
                return []

            sem = asyncio.Semaphore(_SCAN_CONCURRENCY)

            async def _one(t: ScanTopic) -> _ScanOutcome:
                async with sem:
                    try:
                        return await asyncio.wait_for(
                            self._scan_topic_detailed(t.id),
                            timeout=_SCAN_TOPIC_TIMEOUT_S,
                        )
                    except TimeoutError:
                        logger.error("scanner_topic_timeout", topic=t.id)
                        return _ScanOutcome(topic=t, error="timeout")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("scanner_topic_failed", topic=t.id, error=str(exc))
                        return _ScanOutcome(topic=t, error=str(exc)[:200])

            outcomes = list(await asyncio.gather(*[_one(t) for t in topics]))

            posts: list[BlogPost] = []
            for o in outcomes:
                if o.post is not None:
                    posts.append(o.post)

            previous_digest = await self._fetch_previous_post(RUN_DIGEST_TOPIC)
            slug, title, summary, content_md, citations = synthesize_run_digest(
                outcomes, now, previous_digest
            )
            digest_post = BlogPost(
                id=uuid.uuid4(),
                slug=slug,
                topic=RUN_DIGEST_TOPIC,
                title=title,
                summary=summary,
                content_md=content_md,
                tags=["ban-tin-chung", "daily-digest"],
                sources=citations,
                published_at=now,
                created_at=now,
            )
            self._memory_posts.appendleft(digest_post)
            # PASS 2 P3-11: notify must not hold the single-flight lock —
            # persist is quick, channels are dispatched as a detached task.
            await self._persist_digest_only(digest_post)
            self._spawn_notify(digest_post)
            posts.append(digest_post)
            logger.info(
                "scanner_run_digest_complete",
                topics=len(outcomes),
                citations=len(citations),
            )
            return posts

    def start_background_loop(self, interval_hours: float = 24.0) -> None:
        if self._running:
            return
        self._running = True

        async def _loop() -> None:
            logger.info("scanner_background_loop_started", interval_hours=interval_hours)
            while self._running:
                try:
                    # Background runs skip topics refreshed by a recent manual scan.
                    await self.scan_all(respect_last_run=True, interval_hours=interval_hours)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("scanner_loop_error", error=str(exc))
                try:
                    await asyncio.sleep(interval_hours * 3600)
                except asyncio.CancelledError:
                    break

        self._background_task = asyncio.create_task(_loop())

    def stop_background_loop(self) -> None:
        self._running = False
        if self._background_task:
            self._background_task.cancel()
            self._background_task = None
        logger.info("scanner_background_loop_stopped")
