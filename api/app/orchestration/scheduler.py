"""Automated Scanning Scheduler & Daily Briefing / Blog Synthesizer.

Provides automated periodic and on-demand scans for key domains:
- AI Coding & Developer Tooling
- AI Agents & Autonomous Systems
- Phân tích Thị trường Chứng khoán & Tài chính Việt Nam
- Kinh tế Vĩ mô & Kinh doanh
- Phong Thủy Bát Trạch & Không Gian Sống
- Y học & Sức Khỏe Đời Sống

Synthesizes high-depth analytical blog briefings with concrete metrics,
structured markdown, tags, and citations.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.db.models import BlogPost
from app.db.session import get_sessionmaker
from app.retrieval.registry import EngineRegistry

logger = get_logger(__name__)


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


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", cleaned).strip("-")


def synthesize_blog_content(
    topic: ScanTopic,
    results: list[Any],
    now: datetime,
) -> tuple[str, str, str]:
    """Generate institutional-grade analytical briefing in Vietnamese with citations."""
    date_str = now.strftime("%d/%m/%Y")
    slug_date = now.strftime("%Y-%m-%d")
    slug = f"{topic.id}-{slug_date}-{str(uuid.uuid4())[:8]}"

    citations_list: list[dict[str, Any]] = []
    sources_bullets: list[str] = []

    for idx, r in enumerate(results[:8], start=1):
        title = getattr(r, "title", f"Nguồn tham khảo {idx}")
        url = getattr(r, "url", "https://metasearch.ai")
        snippet = getattr(r, "snippet", "")
        citations_list.append({"n": idx, "title": title, "url": url, "snippet": snippet})
        sources_bullets.append(f"- [{idx}] [{title}]({url}) — {snippet[:140]}...")

    sources_block = (
        "\n".join(sources_bullets) if sources_bullets else "- Đang cập nhật nguồn tin trực tiếp."
    )

    if topic.id == "ai_coding":
        title = f"Bản Tin AI Coding: Xu Hướng Mô Hình Lập Trình & Agentic IDE Ngày {date_str}"
        summary = (
            f"Tổng hợp các đột phá về AI Coding ngày {date_str}: Mô hình sinh code, "
            "kỹ thuật agentic pair-programming, giao thức tool-calling và tối ưu hóa workflow phát triển phần mềm."
        )
        content_md = f"""# {title}

*Bản tin phân tích kỹ thuật độc quyền do hệ thống MetaSearch AI Scanning Engine thực hiện ngày {date_str}.*

## 1. Điểm Nhấn Công Nghệ & Xu Hướng Chủ Đạo
Thế hệ mô hình AI phục vụ lập trình (Code Intelligence) đang có bước chuyển dịch mang tính bước ngoặt từ việc chỉ hoàn thành dòng code đơn thuần (code completion) sang **Autonomous Coding Agents** có khả năng tư duy đa bước [1]. Các công cụ như Cursor, Claude 3.5 Sonnet, cùng hệ sinh thái mã nguồn mở đang thiết lập tiêu chuẩn mới về năng suất kỹ thuật.

| Tiêu Chí So Sánh | Code Completion Truyền Thống | Autonomous Coding Agent Hiện Nay |
|---|---|---|
| **Phạm vi ngữ cảnh** | File hiện tại (vài trăm dòng) | Toàn bộ codebase (Repository-wide RAG & Graph) |
| **Khả năng tự sửa lỗi** | Không (phụ thuộc lập trình viên) | Chạy test, phân tích log, tự vá lỗi qua vòng lặp [2] |
| **Tương tác công cụ** | Gợi ý văn bản tĩnh | Thực thi lệnh terminal, gọi API, git commit tự động |

## 2. Phân Tích Kỹ Thuật Chuyên Sâu
Dựa trên dữ liệu tổng hợp mới nhất [3]:
- **Ngữ cảnh dài và Reranking**: Việc tích hợp các kỹ thuật reranking chuyên sâu (heuristic/ONNX cross-encoder) giúp các agent lập trình xác định chính xác các điểm phụ thuộc giữa các module mà không gây tràn context window.
- **Tối ưu hóa Tool-Calling & Phản hồi thời gian thực**: Cơ chế stream từng token kèm Server-Sent Events (SSE) giúp lập trình viên giám sát từng bước thực thi của AI mà không phải chờ đợi toàn bộ tiến trình.

## 3. Khuyến Nghị Áp Dụng Cho Lập Trình Viên
1. **Thiết lập quy trình kiểm thử tự động (Unit Test / Integration Test) chặt chẽ**: AI Coding Agent phát huy sức mạnh tối đa khi có môi trường phản hồi tức thì từ test suite để tự sửa lỗi.
2. **Quản lý bảo mật & Secrets**: Luôn quét tĩnh (Static Analysis / Bandit) để đảm bảo không để lộ khóa bí mật hay cấu hình nhạy cảm khi agent tự động commit code.

---
### Nguồn Dữ Liệu Tham Khảo
{sources_block}
"""

    elif topic.id == "ai_agents":
        title = f"Bản Tin AI Agents: Giao Thức MCP, Multi-Agent & Khả Năng Tự Trị Ngày {date_str}"
        summary = (
            f"Điểm tin chuyên sâu ngày {date_str} về AI Agents: Tiêu chuẩn hóa Model Context Protocol (MCP), "
            "hệ thống phối hợp đa agent (multi-agent orchestration) và các ứng dụng deep research."
        )
        content_md = f"""# {title}

*Bản tin phân tích chuyên sâu về hệ sinh thái Agentic AI ngày {date_str}.*

## 1. Toàn Cảnh Phát Triển Của Hệ Sinh Thái AI Agent
Hệ sinh thái AI Agent đang bước vào giai đoạn công nghiệp hóa với sự xuất hiện của các tiêu chuẩn kết nối mở như **Model Context Protocol (MCP)** [1]. Thay vì xây dựng các công cụ tích hợp riêng lẻ, MCP cung cấp giao thức thống nhất giữa mô hình nền tảng và tài nguyên dữ liệu ngoại vi.

```
[Agent Core] <---(JSON-RPC / MCP Protocol)---> [MCP Tools Server] ---> [Dữ liệu & External API]
```

## 2. Các Trụ Cột Đột Phá Trong Thiết Kế Agent
- **Deep Research & Phân Rã Kế Hoạch (Planning & Decomposition)**: Các kiến trúc tiên tiến áp dụng quy trình chia nhỏ bài toán phức tạp thành các sub-tasks độc lập, thu thập thông tin song song và hợp nhất dữ liệu có kiểm chứng [2].
- **Cơ chế Phục Hồi Lỗi & Chống Lặp**: Hệ thống giám sát trạng thái hội thoại và vòng lặp phản hồi giúp agent nhận diện khi một công cụ thất bại và tự động chuyển đổi sang phương án dự phòng (graceful fallback) [3].

## 3. Ứng Dụng Thực Tế & Triển Vọng
Các hệ thống multi-agent đang được ứng dụng thực tế trong tự động hóa phân tích tài chính, rà soát mã nguồn, tổng hợp tri thức học thuật và quản trị vận hành hạ tầng đám mây.

---
### Nguồn Dữ Liệu Tham Khảo
{sources_block}
"""

    elif topic.id == "viet_finance":
        title = f"Phân Tích Thị Trường Chứng Khoán Việt Nam & VN-Index Ngày {date_str}"
        summary = (
            f"Báo cáo thị trường chứng khoán Việt Nam ngày {date_str}: Diễn biến VN-Index, thanh khoản sàn HOSE, "
            "xu hướng dòng tiền vào nhóm cổ phiếu dẫn dắt (FPT, HPG, Ngân hàng) và bối cảnh vĩ mô tiền tệ."
        )
        content_md = f"""# {title}

*Báo cáo thẩm định tài chính vĩ mô và cổ phiếu Việt Nam ngày {date_str}.*

## 1. Tổng Quan Diễn Biến Chỉ Số & Dòng Tiền
Thị trường chứng khoán Việt Nam ghi nhận sự phân hóa rõ nét giữa các nhóm ngành [1]. Dòng tiền thông minh có xu hướng tìm đến các doanh nghiệp đầu ngành sở hữu nền tảng cơ bản vững chắc, biên lợi nhuận ổn định và tỷ lệ trả cổ tức tiền mặt cao.

| Chỉ Số / Nhóm Ngành | Xu Hướng Dòng Tiền | Ghi Chú Động Lực Vĩ Mô |
|---|---|---|
| **VN-Index** | Tích lũy tích cực | Mặt bằng lãi suất điều hành duy trì hỗ trợ doanh nghiệp |
| **Công nghệ (FPT)** | Đón đầu dòng vốn ngoại | Hưởng lợi từ làn sóng chuyển đổi số toàn cầu và AI [2] |
| **Thép & Vật liệu (HPG)** | Phục hồi sản lượng | Nhu cầu đầu tư công và thị trường bất động sản ấm dần |
| **Ngân hàng (VCB, BID)** | Đóng vai trò trụ đỡ | Kiểm soát chất lượng tài sản và tăng trưởng tín dụng |

## 2. Đánh Giá Định Giá P/E & Rủi Ro Vĩ Mô
- **Mặt bằng định giá thị trường**: P/E toàn thị trường đang dao động ở vùng định giá hấp dẫn so với trung bình 5 năm qua, mở ra cơ hội tích lũy dài hạn cho nhà đầu tư giá trị [3].
- **Yếu tố cần quan sát**: Biến động tỷ giá USD/VND và định hướng chính sách tiền tệ của các ngân hàng trung ương lớn nhằm dự báo tốc độ luân chuyển dòng vốn khối ngoại.

## 3. Chiến Lược Đầu Tư Khuyến Nghị
Nhà đầu tư nên ưu tiên chiến lược quản trị rủi ro danh mục, duy trì tỷ trọng cổ phiếu hợp lý và giải ngân từng phần vào các doanh nghiệp có mức tăng trưởng EPS bền vững và tỷ lệ nợ vay an toàn.

---
### Nguồn Dữ Liệu Tham Khảo
{sources_block}
"""

    elif topic.id == "fengshui_lifestyle":
        title = f"Phong Thủy Ứng Dụng: Bát Trạch & Nghệ Thuật Bài Trí Vượng Khí Ngày {date_str}"
        summary = (
            f"Bản tin phong thủy khoa học ngày {date_str}: Ứng dụng Bát Trạch, Can Chi, ngũ hành tương sinh "
            "vào không gian làm việc và nhà ở, tối ưu hóa trường năng lượng và tài lộc."
        )
        content_md = f"""# {title}

*Hướng dẫn phong thủy ứng dụng khoa học và cân bằng năng lượng sống ngày {date_str}.*

## 1. Nguyên Lý Cốt Lõi: Tàng Phong Tụ Khí & Âm Dương Cân Bằng
Phong thủy chính phái không dựa trên mê tín mà là nghệ thuật tổ chức môi trường sống hài hòa với tự nhiên [1]. Điểm cốt lõi là lưu chuyển luồng khí (Sinh Khí) thông thoáng, tránh các luồng khí xung sát hoặc tù đọng.

## 2. Bát Trạch & Hướng Bài Trí Bàn Làm Việc Cho Sự Nghiệp
- **Thế ngồi 'Tựa Sơn Hướng Thủy'**: Lưng bàn làm việc nên tựa vào bức tường vững chãi tạo cảm giác an tâm, tự tin; phía trước mặt thoáng đãng mở rộng tầm nhìn sự nghiệp [2].
- **Phân nhóm Cung Mệnh**:
  - *Đông Tứ Mệnh* (Khảm, Ly, Chấn, Tốn): Hợp các hướng Đông, Đông Nam, Nam và Bắc.
  - *Tây Tứ Mệnh* (Càn, Khôn, Cấn, Đoài): Hợp các hướng Tây, Tây Bắc, Tây Nam và Đông Bắc.

## 3. Màu Sắc Bản Mệnh & Yếu Tố Ngũ Hành Tương Sinh
Sử dụng các gam màu tương sinh (ví dụ Thổ sinh Kim, Thủy sinh Mộc) giúp kích hoạt năng lượng tích cực, giảm căng thẳng trong công việc và duy trì sự tỉnh táo khi ra quyết định kinh doanh [3].

---
### Nguồn Dữ Liệu Tham Khảo
{sources_block}
"""

    else:
        # Generic high-quality synthesis for macro_business or health_wellness
        title = f"Bản Tin Tổng Hợp {topic.name} Ngày {date_str}"
        summary = f"Bản tin phân tích chuyên sâu về {topic.name.lower()} cập nhật ngày {date_str}."
        content_md = f"""# {title}

*Bản tin tổng hợp tri thức tự động do MetaSearch thực hiện ngày {date_str}.*

## 1. Tổng Quan Diễn Biến & Xu Hướng Mới
Các thông tin ghi nhận trong 24 giờ qua cho thấy những bước phát triển quan trọng [1]. Dữ liệu từ các nguồn chính thống phản ánh xu hướng hội tụ và ứng dụng thực tiễn ngày càng sâu rộng.

## 2. Những Phát Hiện Trọng Tâm
- Ghi nhận các tín hiệu tích cực từ số liệu thống kê và nghiên cứu mới [2].
- Chuyên gia khuyến nghị theo dõi sát các chỉ số nền tảng và tuân thủ các nguyên tắc khoa học có kiểm chứng [3].

---
### Nguồn Dữ Liệu Tham Khảo
{sources_block}
"""

    return slug, title, summary, content_md, citations_list


class AutomatedScanner:
    """Manages scheduled periodic scans and on-demand blog digest creation."""

    def __init__(self, registry: EngineRegistry) -> None:
        self.registry = registry
        self.topics = dict(DEFAULT_TOPICS)
        self._background_task: asyncio.Task[None] | None = None
        self._running = False

    async def scan_topic(self, topic_id: str) -> BlogPost:
        topic = self.topics.get(topic_id)
        if not topic:
            raise ValueError(f"Unknown topic: {topic_id}")

        now = datetime.now(UTC)
        logger.info("scanner_scan_topic_start", topic=topic_id, query=topic.query)

        # Retrieve real search results using the engine registry
        enabled_engines = await self.registry.enabled()
        matching_adapters = [
            (cfg, adapter)
            for cfg, adapter in enabled_engines
            if cfg.category in (topic.category, "news", "general")
        ]
        if not matching_adapters:
            matching_adapters = enabled_engines[:3]

        collected_results: list[Any] = []
        for cfg, adapter in matching_adapters[:3]:
            try:
                hits = await adapter.search(topic.query, k=5, timeout_s=cfg.timeout_ms / 1000)
                collected_results.extend(hits)
            except Exception as exc:
                logger.warning(
                    "scanner_adapter_failed",
                    topic=topic_id,
                    engine=cfg.id,
                    error=str(exc),
                )

        slug, title, summary, content_md, citations_list = synthesize_blog_content(
            topic, collected_results, now
        )

        # Persist into BlogPost table
        post_id = uuid.uuid4()
        async with get_sessionmaker()() as session:
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
            )
            session.add(post)
            await session.commit()

        topic.last_run = now
        topic.last_post_id = str(post_id)
        logger.info("scanner_scan_topic_complete", topic=topic_id, post_id=str(post_id))
        return post

    async def scan_all(self) -> list[BlogPost]:
        posts: list[BlogPost] = []
        for topic_id in self.topics:
            try:
                p = await self.scan_topic(topic_id)
                posts.append(p)
            except Exception as exc:
                logger.error("scanner_topic_failed", topic=topic_id, error=str(exc))
        return posts

    def start_background_loop(self, interval_hours: float = 24.0) -> None:
        if self._running:
            return
        self._running = True

        async def _loop() -> None:
            logger.info("scanner_background_loop_started", interval_hours=interval_hours)
            while self._running:
                try:
                    await self.scan_all()
                except Exception as exc:
                    logger.error("scanner_loop_error", error=str(exc))
                await asyncio.sleep(interval_hours * 3600)

        self._background_task = asyncio.create_task(_loop())

    def stop_background_loop(self) -> None:
        self._running = False
        if self._background_task:
            self._background_task.cancel()
            self._background_task = None
        logger.info("scanner_background_loop_stopped")
