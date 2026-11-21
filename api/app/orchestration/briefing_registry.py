"""Briefing Channel Registry (docs/33 §1).

Single source of truth for every AI-expert-briefing channel (briefing_engine +
/v1/briefings): display name, rolling lookback window, Domain Expert Persona,
engine categories and tags. Adding a new domain channel requires ONE entry
(default here, or one JSON object in the operator override file) - zero code
changes.

Boundary (docs/33 audit P3-7): the scheduler's extractive scan topics
(scheduler.py DEFAULT_TOPICS) remain a SEPARATE legacy layer - they share four
channel ids with this registry but run their own extractive pipeline; both
write BlogPost rows, and /v1/briefings/{id}/latest prefers rows tagged
"ai-briefing" so a scanner digest is never served as an expert briefing.
Frontend preset ids map to registry ids via NewsPanel.channelId.

Zero-fabrication contract carried into every persona: the LLM may only state
claims present in the retrieved evidence; every claim cites its source
number; missing evidence is reported as missing, never invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class BriefingChannel:
    """One expert-briefing channel.

    ``window_hours`` is the STRICT rolling lookback: an article is eligible
    only when its real published_at falls inside now - window_hours (undated
    articles cannot prove recency and are excluded from windowed briefings).
    ``persona_name`` + ``persona_rules`` drive the Step-A synthesis prompt and
    the Step-B council review checklist (docs/33 §3).
    """

    id: str
    name: str
    kind: str  # "news" (core windowed briefing) | "deep" (specialized channel)
    window_hours: int
    persona_name: str
    persona_rules: str
    engine_categories: list[str]
    description: str
    tags: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def freshness_days(self) -> int:
        """Adapter-facing window (adapters take whole days): 48h -> 2, 72h -> 3."""
        return max(1, (self.window_hours + 23) // 24)


# ---------------------------------------------------------------------------
# Core windowed news briefings (Tin Tuc tab - 6 channels)
# ---------------------------------------------------------------------------

_CORE_NEWS_CHANNELS: list[BriefingChannel] = [
    BriefingChannel(
        id="macro_economy",
        name="Tin kinh tế vĩ mô trong 3 ngày gần nhất",
        kind="news",
        window_hours=72,
        persona_name="Chief Macroeconomist",
        persona_rules=(
            "Bạn là Chuyên gia Kinh tế học Vĩ mô Chính (Chief Macroeconomist). "
            "Phân tích tăng trưởng, lạm phát, chính sách tài khóa - tiền tệ, việc làm và thương "
            "mại. "
            "Luôn gắn diễn biến với cơ chế tác động (kênh truyền dẫn chính sách) và chu kỳ kinh tế."
        ),
        engine_categories=["news", "financial"],
        description=(
            "Nhận định vĩ mô 72 giờ: tăng trưởng, lạm phát, chính sách tài khóa - "
            "tiền tệ, việc làm, thương mại."
        ),
        tags=["vi-mo", "kinh-te", "72h"],
    ),
    BriefingChannel(
        id="markets_stocks",
        name="Tin tài chính - chứng khoán trong 2 ngày gần nhất",
        kind="news",
        window_hours=48,
        persona_name="Senior Equity Strategist & Portfolio Manager",
        persona_rules=(
            "Bạn là Chiến lược gia Chứng khoán cao cấp & Quản lý Danh mục "
            "(Senior Equity Strategist & Portfolio Manager). "
            "Phân tích dòng tiền, định giá (P/E, P/B), biến động chỉ số, nhóm ngành dẫn dắt và "
            "luồng vốn nội - ngoại. "
            "Không bao giờ đưa khuyến nghị mua/bán cụ thể; mọi nhận định phải gắn dữ liệu thị "
            "trường "
            "trong bằng chứng."
        ),
        engine_categories=["financial", "news"],
        description=(
            "Nhận định 48 giờ: dòng tiền, định giá, nhóm ngành dẫn dắt, luồng vốn nội - "
            "ngoại và biến động chỉ số."
        ),
        tags=["chung-khoan", "dong-tien", "48h"],
    ),
    BriefingChannel(
        id="business_corporate",
        name="Tin kinh doanh - doanh nghiệp trong 3 ngày gần nhất",
        kind="news",
        window_hours=72,
        persona_name="Corporate Strategy & M&A Analyst",
        persona_rules=(
            "Bạn là Chuyên gia Chiến lược Doanh nghiệp & M&A (Corporate Strategy & M&A Analyst). "
            "Phân tích thương vụ, cấu trúc lại, mở rộng năng lực, hiệu quả kinh doanh và cạnh "
            "tranh ngành. "
            "Đánh giá động cơ chiến lược và hệ quả hợp nhất - cạnh tranh, không suy diễn số tài "
            "chính "
            "ngoài bằng chứng."
        ),
        engine_categories=["news", "financial"],
        description=(
            "Nhận định 72 giờ: thương vụ M&A, cấu trúc lại, mở rộng năng lực, hiệu quả kinh doanh "
            "và cạnh tranh ngành."
        ),
        tags=["kinh-doanh", "ma", "doanh-nghiep", "72h"],
    ),
    BriefingChannel(
        id="tech_ai",
        name="Tin công nghệ - AI trong 3 ngày gần nhất",
        kind="news",
        window_hours=72,
        persona_name="Principal AI & Tech Industry Architect",
        persona_rules=(
            "Bạn là Kiến trúc sư trưởng Công nghệ & AI (Principal AI & Tech Industry Architect). "
            "Phân tích mô hình, hạ tầng tính toán, chuỗi cung ứng chip, sản phẩm và động lực "
            "cạnh tranh "
            "nền tảng. "
            "Phân biệt rõ khả năng thực đã công bố vs. marketing; gắn xu hướng với ràng buộc kỹ "
            "thuật."
        ),
        engine_categories=["developer", "news"],
        description=(
            "Nhận định 72 giờ: mô hình AI, hạ tầng compute, chuỗi cung ứng chip, sản phẩm và "
            "cạnh tranh nền tảng."
        ),
        tags=["cong-nghe", "ai", "72h"],
    ),
    BriefingChannel(
        id="banking_fx",
        name="Tin ngân hàng - tiền tệ trong 3 ngày gần nhất",
        kind="news",
        window_hours=72,
        persona_name="Banking, Credit & Monetary Policy Specialist",
        persona_rules=(
            "Bạn là Chuyên gia Ngân hàng, Tín dụng & Chính sách Tiền tệ "
            "(Banking, Credit & Monetary Policy Specialist). "
            "Phân tích lãi suất điều hành, tín dụng, thanh khoản ngân hàng, tỷ giá và tính ổn định "
            "hệ thống. "
            "Sử dụng đúng thuật ngữ CAMELS/Basel khi đánh giá sức khỏe ngân hàng; không suy diễn "
            "từ "
            "thiếu bằng chứng."
        ),
        engine_categories=["financial", "news"],
        description=(
            "Nhận định 72 giờ: lãi suất điều hành, tín dụng, thanh khoản, tỷ giá và ổn định hệ "
            "thống "
            "ngân hàng."
        ),
        tags=["ngan-hang", "tien-te", "ty-gia", "72h"],
    ),
    BriefingChannel(
        id="global_economy",
        name="Tin kinh tế thế giới trong 3 ngày gần nhất",
        kind="news",
        window_hours=72,
        persona_name="Global Macro & Geopolitical Strategist",
        persona_rules=(
            "Bạn là Chiến lược gia Kinh tế Vĩ mô Toàn cầu & Địa chính trị "
            "(Global Macro & Geopolitical Strategist). "
            "Phân tích liên kết chuỗi cung ứng, thương mại, năng lượng, địa chính trị và tác động "
            "lan tỏa về Việt Nam. "
            "Luôn nêu rõ kênh tác động (thương mại - năng lượng - tài chính) khi liên hệ với nền "
            "kinh tế "
            "trong nước."
        ),
        engine_categories=["news", "financial"],
        description=(
            "Nhận định 72 giờ: chuỗi cung ứng, thương mại, năng lượng, địa chính trị và tác động "
            "lan tỏa về Việt Nam."
        ),
        tags=["the-gioi", "dia-chinh-tri", "72h"],
    ),
]

# ---------------------------------------------------------------------------
# Deep specialized channels (Blog Chuyen Sau tab)
# ---------------------------------------------------------------------------

_DEEP_CHANNELS: list[BriefingChannel] = [
    BriefingChannel(
        id="ai_coding",
        name="AI Coding & LLM Development",
        kind="deep",
        window_hours=96,
        persona_name="Staff AI Engineering & Coding Agent Architect",
        persona_rules=(
            "Bạn là Kiến trúc sư AI Engineering cấp Staff "
            "(Staff AI Engineering & Coding Agent Architect). "
            "Phân tích kiến trúc coding agent, benchmark mô hình code, context management và "
            "công cụ "
            "lập trình. "
            "Đánh giá bằng bằng chứng benchmark công khai; không khẳng định năng lực mô hình ngoài "
            "số liệu trong bằng chứng."
        ),
        engine_categories=["developer", "news"],
        description=(
            "Nhận định chuyên sâu: kiến trúc coding agent, benchmark code, context management, "
            "IDE và "
            "công cụ lập trình."
        ),
        tags=["ai-coding", "llm", "developer-tools"],
    ),
    BriefingChannel(
        id="ai_agents",
        name="AI Agents & MCP",
        kind="deep",
        window_hours=96,
        persona_name="Autonomous Agent Systems & Protocol Architect",
        persona_rules=(
            "Bạn là Kiến trúc sư Hệ thống Agent tự trị & Giao thức "
            "(Autonomous Agent Systems & Protocol Architect). "
            "Phân tích kiến trúc multi-agent, chuẩn giao tiếp MCP, tool calling, an toàn agent và "
            "deep research pipeline. "
            "Tách bạch yếu tố kỹ thuật khỏi tuyên bố quảng cáo; mọi khẳng định kiến trúc phải có "
            "nguồn trong bằng chứng."
        ),
        engine_categories=["developer", "news"],
        description=(
            "Nhận định chuyên sâu: multi-agent, MCP, tool calling, an toàn agent và pipeline "
            "deep research."
        ),
        tags=["ai-agents", "mcp", "multi-agent"],
    ),
    BriefingChannel(
        id="vietnam_stocks",
        name="Chứng Khoán VN & VN-Index",
        kind="deep",
        window_hours=72,
        persona_name="Vietnam Market Strategist — HOSE/HNX/Foreign Flow",
        persona_rules=(
            "Bạn là Chiến lược gia Thị trường Việt Nam "
            "(Vietnam Market Strategist — HOSE/HNX/Foreign Flow). "
            "Phân tích VN-Index/VN30, thanh khoản HOSE/HNX, dòng khối ngoại, nhóm ngành dẫn dắt và "
            "chính sách thị trường. "
            "Không khuyến nghị mua/bán; không suy diễn số liệu ngoài bằng chứng; ghi rõ phiên/ngày "
            "của dữ liệu."
        ),
        engine_categories=["financial", "news"],
        description=(
            "Nhận định chuyên sâu: VN-Index/VN30, thanh khoản HOSE/HNX, khối ngoại, nhóm ngành và "
            "chính sách thị trường."
        ),
        tags=["chung-khoan-vn", "vn-index", "ho-se", "khoi-ngoai"],
    ),
    BriefingChannel(
        id="fengshui_lifestyle",
        name="Phong Thủy Ứng Dụng",
        kind="deep",
        window_hours=168,
        persona_name=(
            "Classical Eastern Metaphysics & Practical Feng Shui Researcher — "
            "grounded, analytical, non-superstitious"
        ),
        persona_rules=(
            "Bạn là Nghiên cứu siêu hình học phương Đông cổ điển & Phong thủy ứng dụng "
            "(Classical Eastern Metaphysics & Practical Feng Shui Researcher) — tư duy phân tích, "
            "KHÔNG mê tín: trình bày Bát Trạch, Can Chi, Ngũ Hành như hệ thống nguyên tắc bố trí "
            "không gian truyền thống, nêu rõ đây là quan niệm văn hóa, không phải y học hay tài "
            "chính. "
            "Mọi nguyên lý phải trích từ nguồn trong bằng chứng; không hứa hẹn kết quả tài lộc "
            "cụ thể."
        ),
        engine_categories=["fengshui", "news"],
        description=(
            "Nhận định chuyên sâu: Bát Trạch, Can Chi, Ngũ Hành nạp âm ứng dụng vào bố trí không "
            "gian — "
            "phân tích, không mê tín."
        ),
        tags=["phong-thuy", "bat-trach", "ngu-hanh"],
    ),
    BriefingChannel(
        id="health_wellness",
        name="Y Học & Sức Khỏe",
        kind="deep",
        window_hours=168,
        persona_name=(
            "Evidence-Based Preventive Medicine & Longevity Specialist — "
            "strictly citing research, never diagnosing"
        ),
        persona_rules=(
            "Bạn là Chuyên gia Y học dự phòng dựa trên bằng chứng & Longevity "
            "(Evidence-Based Preventive Medicine & Longevity Specialist). "
            "CHỈ trích dẫn kết quả nghiên cứu trong bằng chứng kèm nguồn; TUYỆT ĐỐI không chẩn "
            "đoán, "
            "không kê đơn, không tư vấn cá nhân hóa; luôn khuyến nghị tham khảo ý kiến bác sĩ khi "
            "nội dung chạm y khoa lâm sàng."
        ),
        engine_categories=["academic", "news"],
        description=(
            "Nhận định chuyên sâu: nghiên cứu y học dự phòng và longevity — trích nghiên cứu, "
            "không chẩn đoán, không kê đơn."
        ),
        tags=["y-hoc", "suc-khoe", "longevity"],
    ),
]

BRIEFING_CHANNELS: dict[str, BriefingChannel] = {
    ch.id: ch for ch in (*_CORE_NEWS_CHANNELS, *_DEEP_CHANNELS)
}

# Deep-tab display order ("Tất cả" và "Bản tin chung" render qua scan_all /
# run digest có sẵn — không phải channel trong registry).
DEEP_CHANNEL_ORDER: list[str] = [
    "ai_coding",
    "ai_agents",
    "vietnam_stocks",
    "macro_economy",
    "fengshui_lifestyle",
    "health_wellness",
]

NEWS_CHANNEL_ORDER: list[str] = [ch.id for ch in _CORE_NEWS_CHANNELS]


def load_channel_overrides(override_file: str | Path | None) -> dict[str, BriefingChannel]:
    """Merge operator-defined channels from a JSON file (docs/33 §1: adding a
    domain requires ONE JSON entry, zero code changes). Each object accepts
    the same fields as :class:`BriefingChannel` (``kind`` defaults to
    ``"deep"``). Entries with an existing id REPLACE the built-in channel
    (persona/window overrides); invalid entries are skipped with a warning —
    one bad row never breaks the registry."""
    channels = dict(BRIEFING_CHANNELS)
    if not override_file:
        return channels
    path = Path(override_file)
    if not path.is_file():
        logger.warning("briefing_override_missing", path=str(path))
        return channels
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("briefing_override_parse_failed", error=type(exc).__name__)
        return channels
    rows = payload.get("channels") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        logger.warning("briefing_override_bad_shape")
        return channels
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        try:
            ch = BriefingChannel(
                id=str(row["id"])[:64],
                name=str(row.get("name") or row["id"])[:120],
                kind=str(row.get("kind") or "deep")[:16],
                window_hours=max(1, int(row.get("window_hours") or 72)),
                persona_name=str(row.get("persona_name") or "Domain Expert")[:120],
                persona_rules=str(row.get("persona_rules") or "")[:2000],
                engine_categories=[
                    str(c)[:24] for c in (row.get("engine_categories") or ["news"])[:4]
                ],
                description=str(row.get("description") or "")[:400],
                tags=[str(t)[:40] for t in (row.get("tags") or [])[:8]],
                enabled=bool(row.get("enabled", True)),
            )
        except Exception as exc:
            logger.warning("briefing_override_row_skipped", error=type(exc).__name__)
            continue
        channels[ch.id] = ch
    return channels


def channel_to_topic_view(ch: BriefingChannel) -> dict[str, Any]:
    """Registry introspection payload for GET /v1/briefings/channels."""
    return {
        "id": ch.id,
        "name": ch.name,
        "kind": ch.kind,
        "window_hours": ch.window_hours,
        "persona_name": ch.persona_name,
        "description": ch.description,
        "tags": ch.tags,
        "enabled": ch.enabled,
        "freshness_days": ch.freshness_days,
    }
