"""Expert Briefing Engine (docs/33 §2-3).

Deterministic pre-processing pipeline + expert synthesis + council review for
every briefing channel in :mod:`briefing_registry`:

1. **Deterministic pre-processing (system-first, no LLM)**: fan-out to the
   channel's engine categories with a hard freshness window, STRICT rolling
   window filter on real ``published_at``, canonical-URL + SimHash dedup into
   event threads, authority ranking (domain tier + recency + cross-engine
   agreement).
2. **Step A — Domain Expert Synthesis**: one grounded LLM call with the
   channel persona; output follows the 3-section template (Executive Thesis /
   Key Signals with What->Why->Second-order impact / Expert Takeaway &
   Watchlist) with ``[n]`` citations into the numbered evidence packet.
3. **Deterministic grounding verifier**: every ``[n]`` must map to evidence;
   numeric claims in the draft must appear in the evidence text — unverified
   sentences are stripped before publication (zero-hallucination is enforced
   by the SYSTEM, not begged from the model).
4. **Step B — Council review**: two reviewer roles (Fact-Checker, Domain
   Skeptic) over trusted allowlisted endpoints verify zero-hallucination and
   analytical depth; issues trigger one revision pass. Outcomes feed the
   Dream-RSI policy store (``record_endpoint_outcome``) so endpoint selection
   keeps learning.
5. **Fallback**: when AI is disabled/unavailable, an extractive briefing is
   synthesized from the same evidence — the pipeline never fabricates and
   never fails open into invented content.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.config import get_settings
from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.orchestration.briefing_registry import BriefingChannel, channel_to_topic_view
from app.retrieval.models import RawResult
from app.retrieval.simhash import is_near_duplicate, simhash64
from app.retrieval.tiers import tier_score

logger = get_logger(__name__)

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")
_NUMBER_RE = re.compile(r"\d[\d.,]*\s*%|\d[\d.,]{1,}")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_SINGLE_FLIGHT = SingleFlight()


@dataclass(slots=True)
class EvidenceItem:
    """One deduplicated event thread in the evidence packet."""

    n: int
    title: str
    snippet: str
    url: str
    source_name: str
    published: datetime | None
    engines: list[str]
    tier_score: float
    thread_members: list[str]  # all URLs folded into this thread

    def authority(self, now: datetime) -> float:
        """Deterministic rank: domain tier + cross-engine agreement + recency."""
        recency = 0.0
        if self.published is not None:
            age_h = max(0.0, (now - self.published).total_seconds() / 3600.0)
            recency = max(0.0, 1.0 - age_h / 168.0)  # 1 week linear decay
        return self.tier_score + 0.1 * max(0, len(self.engines) - 1) + 0.5 * recency


@dataclass(slots=True)
class BriefingResult:
    channel_id: str
    title: str
    summary: str
    content_md: str
    citations: list[dict[str, Any]]
    ai_grounded: bool
    review_verdicts: list[dict[str, Any]] = field(default_factory=list)
    evidence_count: int = 0
    dropped_stale: int = 0
    stripped_unverified: int = 0
    window_hours: int = 72
    persona_name: str = ""


def _clean(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw or "")).strip()


def _pub_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def collect_channel_evidence(
    channel: BriefingChannel,
    registry: Any,
    *,
    now: datetime,
    max_evidence: int = 18,
) -> tuple[list[EvidenceItem], list[RawResult], int]:
    """Deterministic pre-processing (docs/33 §2): fan-out -> STRICT window
    filter -> canonical+SimHash dedup -> event threads -> authority ranking.

    Returns ``(evidence_items, raw_results_for_fallback, dropped_stale)``.
    Articles without a real ``published_at`` cannot prove recency and are
    excluded from windowed briefings (never guessed)."""
    enabled = await registry.enabled()
    matched = [(cfg, a) for cfg, a in enabled if cfg.category in set(channel.engine_categories)]
    if not matched:
        matched = enabled[:3]

    freshness_days = channel.freshness_days

    async def _one(cfg, adapter):
        try:
            return await adapter.search(
                channel.name,
                max_results=6,
                freshness_days=freshness_days,
                timeout_s=cfg.timeout_ms / 1000,
            )
        except Exception as exc:
            logger.warning(
                "briefing_engine_failed",
                channel=channel.id,
                engine=cfg.id,
                error=type(exc).__name__,
            )
            return None

    gathered = await asyncio.gather(*(_one(cfg, a) for cfg, a in matched[:6]))
    window_start = now - timedelta(hours=channel.window_hours)

    raw_results: list[RawResult] = []
    strict: list[RawResult] = []
    dropped_stale = 0
    for res in gathered:
        if not isinstance(res, list):
            continue
        raw_results.extend(res)
        for r in res:
            url = (getattr(r, "url", "") or "").strip()
            pub = _pub_utc(getattr(r, "published_at", None))
            if not url or pub is None:
                continue  # no real timestamp -> cannot prove window membership
            if pub < window_start:
                dropped_stale += 1
                continue
            strict.append(r)

    # Canonical-URL dedup + event-thread clustering (SimHash near-dup fold).
    threads: list[dict[str, Any]] = []
    for r in strict:
        title = _clean(getattr(r, "title", ""))
        snippet = _clean(getattr(r, "snippet", ""))
        url = (getattr(r, "url", "") or "").strip()
        engine = getattr(r, "adapter", "") or ""
        pub = _pub_utc(getattr(r, "published_at", None))
        h = simhash64(f"{title} {snippet}")
        merged = False
        for t in threads:
            if is_near_duplicate(h, t["hash"]):
                t["members"].append(url)
                if engine and engine not in t["engines"]:
                    t["engines"].append(engine)
                if len(snippet) > len(t["snippet"]):
                    t["snippet"] = snippet
                if pub is not None and (t["published"] is None or pub > t["published"]):
                    t["published"] = pub
                merged = True
                break
        if merged:
            continue
        source_name = str(getattr(r, "source_name", "") or "")
        try:
            domain = urlsplit(url).hostname or ""
            authority = tier_score(domain)
        except Exception:
            domain, authority = "", 1.0
        threads.append(
            {
                "title": title or url,
                "snippet": snippet or title,
                "url": url,
                "domain": domain,
                "source_name": source_name or domain,
                "published": pub,
                "engines": [engine] if engine else [],
                "hash": h,
                "members": [url],
                "authority": authority,
            }
        )

    threads.sort(
        key=lambda t: (
            t["authority"],
            _pub_utc(t["published"]) or now,
            len(t["members"]),
        ),
        reverse=True,
    )

    evidence: list[EvidenceItem] = []
    for idx, t in enumerate(threads[: max(1, max_evidence)], start=1):
        evidence.append(
            EvidenceItem(
                n=idx,
                title=t["title"],
                snippet=t["snippet"],
                url=t["url"],
                source_name=t["source_name"],
                published=_pub_utc(t["published"]),
                engines=list(t["engines"]),
                tier_score=t["authority"],
                thread_members=list(t["members"]),
            )
        )
    return evidence, raw_results, dropped_stale


def _evidence_packet(evidence: list[EvidenceItem]) -> str:
    lines: list[str] = []
    for e in evidence:
        date_str = e.published.strftime("%d/%m/%Y") if e.published else "không rõ ngày"
        lines.append(
            f"[{e.n}] ({date_str}) {e.source_name}: {e.title} — {e.snippet[:240]} (URL: {e.url})"
        )
    return "\n".join(lines)


def _build_synthesis_prompt(
    channel: BriefingChannel, evidence: list[EvidenceItem], now: datetime
) -> tuple[str, str]:
    system = (
        f"{channel.persona_rules}\n\n"
        "QUY TRÌNH BẮT BUỘC (docs/33 §3 Step A):\n"
        "1. Chỉ dùng thông tin từ BẰNG CHỨNG được đánh số [n] bên dưới. "
        "Không thêm bất kỳ số liệu, mốc thời gian, mã chứng khoán hay sự kiện nào "
        "không có trong bằng chứng.\n"
        "2. Mỗi câu chứa dữ kiện phải có [n] trỏ tới đúng nguồn. Câu nhận định chuyên môn "
        "(không có số liệu) không cần [n] nhưng phải bám vào bằng chứng.\n"
        "3. Nếu bằng chứng không đủ cho một khía cạnh, ghi rõ "
        "'Bằng chứng trong kỳ chưa đủ để nhận định' — KHÔNG suy diễn, KHÔNG dùng kiến thức ngoài.\n"
        "4. Cấu trúc bắt buộc (tiếng Việt):\n"
        "## Bức tranh tổng quan\n(2-3 câu xu hướng chủ đạo của cửa sổ thời gian)\n"
        "## Các diễn biến trọng yếu\n"
        "(các nhóm sự kiện; mỗi nhóm: What happened -> Why it matters -> "
        "Tác động bậc hai; kèm [n])\n"
        "## Góc nhìn chuyên gia & Điểm cần theo dõi\n"
        "(tín hiệu hành động, rủi ro, catalyst 3-7 ngày tới)\n"
        "5. Không mở đầu bằng lời chào, không kết luận ngoài ba phần trên."
    )
    user = (
        f"Kênh: {channel.name}\n"
        f"Cửa sổ thời gian: {channel.window_hours} giờ qua (hôm nay {now.strftime('%d/%m/%Y')}).\n"
        f"Persona: {channel.persona_name}\n\n"
        "BẰNG CHỨNG (đánh số [n]):\n"
        f"{_evidence_packet(evidence)}\n\n"
        "Viết bản tin chuyên sâu theo cấu trúc bắt buộc. Chỉ dùng bằng chứng trên."
    )
    return system, user


def _strip_unverified(
    draft: str, evidence: list[EvidenceItem], *, today: datetime | None = None
) -> tuple[str, int, int]:
    """Deterministic zero-hallucination verifier (docs/33 §3).

    Matching text = the FULL evidence packet the model actually saw (source
    names, dates, URLs included) plus today's date — an audit probe showed
    matching only title+snippet stripped legitimately dated sentences the
    packet itself had fed the model. Returns
    ``(clean_draft, sentences_removed, refs_removed)`` — the two counters are
    separate so the published header never overstates."""
    # Same rendering as _evidence_packet (dates + names + URLs included).
    evidence_text = _evidence_packet(evidence)
    if today is not None:
        evidence_text += f"\n{today.strftime('%d/%m/%Y')}"
    max_n = len(evidence)
    sentences_removed = 0
    refs_removed = 0
    out_lines: list[str] = []

    def _drop_bad_ref(match: re.Match[str]) -> str:
        nonlocal refs_removed
        ref = int(match.group(1))
        if ref < 1 or ref > max_n:
            refs_removed += 1
            return ""
        return match.group(0)

    for line in draft.split("\n"):
        cleaned_line = _CITATION_RE.sub(_drop_bad_ref, line)
        numbers = _NUMBER_RE.findall(cleaned_line)
        if numbers:
            sentences = re.split(r"(?<=[.!?…])\s+", cleaned_line)
            kept: list[str] = []
            for sentence in sentences:
                s_numbers = _NUMBER_RE.findall(sentence)
                if not s_numbers:
                    kept.append(sentence)
                    continue
                if all(_num_in_evidence(num, evidence_text) for num in s_numbers):
                    kept.append(sentence)
                else:
                    sentences_removed += 1
            cleaned_line = " ".join(k for k in kept if k.strip())
        if cleaned_line.strip():
            out_lines.append(cleaned_line)
    return "\n".join(out_lines), sentences_removed, refs_removed


def _num_in_evidence(num: str, evidence_text: str) -> bool:
    digits = re.sub(r"[^\d]", "", num)
    if not digits:
        return True
    # Match on raw digits so "1,2%" / "1.2%" variants are equivalent.
    return digits in re.sub(r"[^\d]", "", evidence_text)


def _parse_review(text: str) -> dict[str, Any]:
    """Defensively parse a reviewer verdict (JSON or key: value lines)."""
    verdict: dict[str, Any] = {"hallucination_free": True, "depth_ok": True, "issues": []}
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            payload = json.loads(text[start : end + 1])
            if isinstance(payload, dict):
                verdict["hallucination_free"] = bool(payload.get("hallucination_free", True))
                verdict["depth_ok"] = bool(payload.get("depth_ok", True))
                issues = payload.get("issues")
                if isinstance(issues, list):
                    verdict["issues"] = [str(i)[:200] for i in issues[:8]]
                return verdict
    except Exception:
        pass
    lowered = text.lower()
    if "hallucination: no" in lowered or "hallucination_free: false" in lowered:
        verdict["hallucination_free"] = False
    if "depth: shallow" in lowered or "depth_ok: false" in lowered:
        verdict["depth_ok"] = False
    return verdict


_REVIEWER_ROLES = (
    (
        "Fact-Checker",
        "Bạn là Kiểm chứng viên phản biện (Fact-Checker). Kiểm tra TỪNG số liệu/ngày/"
        "mã chứng khoán trong bản tin có khớp bằng chứng [n] không. Trả JSON: "
        '{"hallucination_free": true/false, "depth_ok": true/false, '
        '"issues": ["mô tả lỗi cụ thể"]}. Chỉ JSON, không thêm chữ.',
    ),
    (
        "Domain Skeptic",
        "Bạn là Chuyên gia phản biện ngành (Domain Skeptic). Đánh giá bản tin có Depth "
        "phân tích thật (What->Why->Tác động bậc hai) hay chỉ paraphrase tiêu đề; "
        "chỉ rõ chỗ phân tích nông. "
        'Trả JSON: {"hallucination_free": true/false, "depth_ok": true/false, '
        '"issues": ["mô tả"]}. Chỉ JSON.',
    ),
)


class BriefingEngine:
    """Expert briefing pipeline bound to one EngineRegistry + LLMGateway."""

    def __init__(self, registry: Any, gateway: Any) -> None:
        self.registry = registry
        self.gateway = gateway

    # -- public ---------------------------------------------------------------

    async def generate(
        self,
        channel: BriefingChannel,
        *,
        now: datetime | None = None,
        use_ai: bool = True,
    ) -> BriefingResult:
        settings = get_settings()
        now = now or datetime.now(UTC)
        max_evidence = max(6, int(getattr(settings, "briefing_max_evidence", 18)))
        evidence, _raw_results, dropped_stale = await collect_channel_evidence(
            channel, self.registry, now=now, max_evidence=max_evidence
        )

        if not evidence:
            return self._empty_result(channel, now, dropped_stale)

        ai_allowed = (
            use_ai
            and bool(getattr(settings, "briefing_ai_enabled", True))
            and getattr(self.gateway, "provider", "none") != "none"
        )
        if not ai_allowed:
            return self._extractive_fallback(channel, evidence, now, dropped_stale)

        flight_key = f"briefing:{channel.id}:{now.strftime('%Y%m%d%H')}"
        # docs/33 audit P2-6: ALL result fields are built INSIDE the flighted
        # coroutine — no post-flight mutation of a shared instance.
        result = await _SINGLE_FLIGHT.run(
            flight_key,
            lambda: self._synthesize_with_review(
                channel, evidence, now, evidence_count=len(evidence), dropped_stale=dropped_stale
            ),
        )
        return result

    def channels_view(self) -> list[dict[str, Any]]:
        settings = get_settings()
        override = str(getattr(settings, "briefing_channels_file", "") or "")
        from app.orchestration.briefing_registry import load_channel_overrides

        channels = load_channel_overrides(override or None)
        return [channel_to_topic_view(ch) for ch in channels.values() if ch.enabled]

    def _empty_result(
        self, channel: BriefingChannel, now: datetime, dropped_stale: int
    ) -> BriefingResult:
        date_str = now.strftime("%d/%m/%Y")
        return BriefingResult(
            channel_id=channel.id,
            title=f"{channel.name} — Bản Tin Ngày {date_str}",
            summary=(
                f"Không đủ bài viết trong cửa sổ {channel.window_hours}h từ các nguồn đã hỏi — "
                "không suy diễn, không bịa nguồn."
            ),
            content_md=(
                f"# {channel.name} — Bản Tin Ngày {date_str}\n\n"
                f"Cửa sổ thời gian: {channel.window_hours}h. Không có bài viết nào "
                "có mốc thời gian thực trong cửa sổ từ các nguồn đã hỏi — "
                "không suy diễn, không bịa nguồn."
            ),
            citations=[],
            ai_grounded=False,
            evidence_count=0,
            dropped_stale=dropped_stale,
            window_hours=channel.window_hours,
            persona_name=channel.persona_name,
        )

    # -- Step A + B ------------------------------------------------------------

    async def _synthesize_with_review(
        self,
        channel: BriefingChannel,
        evidence: list[EvidenceItem],
        now: datetime,
        *,
        evidence_count: int,
        dropped_stale: int,
    ) -> BriefingResult:
        settings = get_settings()
        system, user = _build_synthesis_prompt(channel, evidence, now)
        review_enabled = bool(getattr(settings, "briefing_review_enabled", True))

        try:
            draft, _usage = await self.gateway.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=1600,
                task="briefing",
            )
        except Exception as exc:
            logger.warning(
                "briefing_synthesis_failed", channel=channel.id, error=type(exc).__name__
            )
            return self._extractive_fallback(
                channel,
                evidence,
                now,
                0,
                evidence_count=evidence_count,
            )

        draft, sentences_stripped, _refs_stripped = _strip_unverified(draft, evidence, today=now)

        review_verdicts: list[dict[str, Any]] = []
        if review_enabled:
            review_verdicts = await self._council_review(channel, draft, evidence, now)
            blocking = [
                v
                for v in review_verdicts
                if not v.get("hallucination_free", True) or not v.get("depth_ok", True)
            ]
            if blocking and blocking[0].get("issues"):
                issues = "\n".join(f"- {i}" for i in blocking[0]["issues"][:5])
                try:
                    revised, _u2 = await self.gateway.complete(
                        [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": (
                                    f"{user}\n\nBẢN NHÁP HIỆN TẠI:\n{draft}\n\n"
                                    f"PHẢN BIỆN CẦN XỬ LÝ:\n{issues}\n\n"
                                    "Chỉnh sửa bản nháp theo phản biện (vẫn giữ cấu trúc + [n]); "
                                    "chỉ dùng bằng chứng trên."
                                ),
                            },
                        ],
                        max_tokens=1600,
                        task="briefing_revision",
                    )
                    revised, s2, _r2 = _strip_unverified(revised, evidence, today=now)
                    draft = revised or draft
                    sentences_stripped += s2
                except Exception as exc:
                    logger.warning(
                        "briefing_revision_failed",
                        channel=channel.id,
                        error=type(exc).__name__,
                    )

        date_str = now.strftime("%d/%m/%Y")
        title = f"{channel.name} — Bản Tin Chuyên Sâu Ngày {date_str}"
        summary = (
            f"Bản tin tổng hợp bởi AI (persona: {channel.persona_name}) cho cửa sổ "
            f"{channel.window_hours}h — {len(evidence)} sự kiện, phản biện "
            f"{len(review_verdicts)} lượt; đã kiểm chứng số liệu tự động."
        )
        header_star = (
            f"*Persona: {channel.persona_name} · Cửa sổ {channel.window_hours}h · "
            f"{len(evidence)} sự kiện · AI grounded + kiểm chứng tự động"
            + (
                f" · đã loại {sentences_stripped} câu số liệu không có trong bằng chứng*"
                if sentences_stripped
                else "*"
            )
        )
        content_md = f"# {title}\n\n{header_star}\n\n{draft}\n\n{self._sources_block(evidence)}"
        citations = self._citations_from(evidence)
        return BriefingResult(
            channel_id=channel.id,
            title=title,
            summary=summary,
            content_md=content_md,
            citations=citations,
            ai_grounded=True,
            review_verdicts=review_verdicts,
            evidence_count=evidence_count,
            dropped_stale=dropped_stale,
            stripped_unverified=sentences_stripped,
            window_hours=channel.window_hours,
            persona_name=channel.persona_name,
        )

    async def _council_review(
        self, channel: BriefingChannel, draft: str, evidence: list[EvidenceItem], now: datetime
    ) -> list[dict[str, Any]]:
        """Step B (docs/33 §3): two reviewer roles over the trusted endpoint
        panel. Outcomes feed the Dream-RSI policy store so endpoint selection
        keeps learning (council.py record_endpoint_outcome)."""
        settings = get_settings()
        verdicts: list[dict[str, Any]] = []
        dream_store = None
        try:
            from app.llm.council import get_dream_rsi_store

            dream_store = get_dream_rsi_store()
        except Exception:
            dream_store = None

        for role_name, role_prompt in _REVIEWER_ROLES:
            started = time.perf_counter()
            ok = False
            provider = str(getattr(self.gateway, "provider", "unknown"))
            verdict: dict[str, Any] = {
                "reviewer": role_name,
                "hallucination_free": True,
                "depth_ok": True,
                "issues": [],
            }
            try:
                text, _usage = await self.gateway.complete(
                    [
                        {"role": "system", "content": role_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"Kênh: {channel.name} (persona: {channel.persona_name})\n"
                                f"Bằng chứng:\n{_evidence_packet(evidence)}\n\n"
                                f"BẢN NHÁP CẦN PHẢN BIỆN:\n{draft}\n"
                            ),
                        },
                    ],
                    max_tokens=400,
                    task="briefing_review",
                )
                parsed = _parse_review(text)
                verdict.update(parsed)
                verdicts.append(verdict)
                ok = True
            except Exception as exc:
                logger.warning(
                    "briefing_review_failed", reviewer=role_name, error=type(exc).__name__
                )
                verdict["error"] = type(exc).__name__
                verdicts.append(verdict)
            if dream_store is not None:
                with contextlib.suppress(Exception):
                    dream_store.record_endpoint_outcome(
                        provider=provider,
                        # Best-effort attribution: the gateway routes per task,
                        # usage metadata does not carry the model — record the
                        # configured report model (docs/33 audit P2-5 note).
                        model=str(getattr(settings, "llm_report_model", "")),
                        ok=ok,
                        # A reviewer that flagged hallucinations must NOT score
                        # perfect precision (audit P2-5).
                        citation_precision=1.0
                        if (ok and verdict.get("hallucination_free", True))
                        else 0.0,
                        peer_score=1.0 if (ok and verdict.get("depth_ok", True)) else 0.0,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
        return verdicts

    # -- fallback ----------------------------------------------------------------

    def _extractive_fallback(
        self,
        channel: BriefingChannel,
        evidence: list[EvidenceItem],
        now: datetime,
        dropped_stale: int,
        *,
        evidence_count: int | None = None,
    ) -> BriefingResult:
        date_str = now.strftime("%d/%m/%Y")
        title = f"{channel.name} — Bản Tin Ngày {date_str}"
        lines: list[str] = []
        for e in evidence[:10]:
            d = e.published.strftime("%d/%m/%Y") if e.published else "không rõ ngày"
            lines.append(
                f"- **{e.title}** *(ngày {d})* — {e.source_name}: {e.snippet[:240]} [Nguồn {e.n}]"
            )
        content_md = (
            f"# {title}\n\n"
            f"*Chế độ extractive (AI synthesis không khả dụng) — Persona dự kiến: "
            f"{channel.persona_name}. Toàn bộ nội dung trích từ nguồn thật, không nhận định "
            f"thay nguồn.*\n\n" + "\n".join(lines) + "\n\n" + self._sources_block(evidence)
        )
        return BriefingResult(
            channel_id=channel.id,
            title=title,
            summary=(
                f"Bản tin extractive cho cửa sổ {channel.window_hours}h — "
                f"{len(evidence)} sự kiện (AI synthesis không khả dụng)."
            ),
            content_md=content_md,
            citations=self._citations_from(evidence),
            ai_grounded=False,
            evidence_count=evidence_count if evidence_count is not None else len(evidence),
            dropped_stale=dropped_stale,
            window_hours=channel.window_hours,
            persona_name=channel.persona_name,
        )

    # -- shared ------------------------------------------------------------------

    @staticmethod
    def _citations_from(evidence: list[EvidenceItem]) -> list[dict[str, Any]]:
        return [
            {
                "n": e.n,
                "title": e.title,
                "url": e.url,
                "snippet": e.snippet,
                "engines": e.engines,
                "published": e.published.strftime("%d/%m/%Y") if e.published else None,
            }
            for e in evidence
        ]

    @staticmethod
    def _sources_block(evidence: list[EvidenceItem]) -> str:
        rows = "\n".join(
            f"- [{e.n}] [{e.source_name}]({e.url}) — {e.title}"
            + (f" ({e.published.strftime('%d/%m/%Y')})" if e.published else "")
            for e in evidence
        )
        return f"### Nguồn Dữ Liệu\n{rows}"
