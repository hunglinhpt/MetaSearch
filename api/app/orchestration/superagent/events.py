"""Proactive Event Ingestion & Semantic Filter Engine.

Listens to real-time events (market announcements, BCTC filings, GitHub triggers,
CVE alerts) and autonomously spawns Super-Agent tickets under Observe or Live mode.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logging import get_logger
from app.orchestration.superagent.models import (
    ExecutionMode,
    SuperAgentEvent,
    SuperAgentTicket,
    TicketPriority,
)
from app.orchestration.superagent.router import SuperAgentRouter

logger = get_logger(__name__)


class EventIngestionEngine:
    """Ingests asynchronous events and triggers proactive Super-Agent tickets."""

    def __init__(
        self,
        router: SuperAgentRouter,
        *,
        default_mode: ExecutionMode = ExecutionMode.OBSERVE,
    ) -> None:
        self.router = router
        self.default_mode = default_mode

    def ingest(
        self,
        *,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        title: str = "",
        auto_spawn_ticket: bool = True,
    ) -> tuple[SuperAgentEvent, SuperAgentTicket | None]:
        """Ingest event, run semantic filter, and optionally spawn a ticket."""
        event_title = title or payload.get("title") or f"Event from {source}: {event_type}"
        event = SuperAgentEvent(
            source=source,
            event_type=event_type,
            payload=payload,
            title=event_title,
            timestamp=time.time(),
        )
        self.router.register_event(event)

        ticket: SuperAgentTicket | None = None
        if auto_spawn_ticket:
            ticket = self._evaluate_and_spawn(event)

        return event, ticket

    def _evaluate_and_spawn(self, event: SuperAgentEvent) -> SuperAgentTicket | None:
        """Semantic filter: Evaluate urgency and map event payload to an autonomous ticket."""
        source = event.source.lower()
        evt_type = event.event_type.lower()
        payload = event.payload

        priority = TicketPriority.NORMAL
        domain = "auto"
        objective = ""

        if "market" in source or "viet_stock" in source or "sec" in source:
            domain = "financial"
            symbol = payload.get("symbol") or payload.get("ticker") or "thị trường"
            news_text = payload.get("summary") or payload.get("text") or event.title
            objective = (
                f"Phân tích pháp y BCTC và tác động kỹ thuật đối với mã {symbol}: {news_text}"
            )
            if "bctc" in evt_type or "earnings" in evt_type:
                priority = TicketPriority.HIGH

        elif "github" in source or "cve" in source or "tech" in source:
            domain = "technology_ai"
            issue_title = payload.get("issue_title") or payload.get("repo") or event.title
            issue_body = payload.get("body") or payload.get("details") or ""
            objective = (
                f"Kiểm tra bảo mật, phân tích nguyên nhân gốc rễ cho: {issue_title}. "
                f"Chi tiết: {issue_body[:300]}"
            )
            if "security" in evt_type or "vulnerability" in evt_type or "cve" in evt_type:
                priority = TicketPriority.HIGH

        elif "biomedical" in source or "pubmed" in source or "health" in source:
            domain = "biomedical_health"
            trial_topic = payload.get("topic") or event.title
            objective = f"Tổng hợp bằng chứng lâm sàng PICO và đánh giá thử nghiệm: {trial_topic}"

        else:
            domain = "general_discovery"
            objective = f"Tìm kiếm chuyên sâu và kiểm chứng đa nguồn thông tin: {event.title}"

        ticket = self.router.submit_goal(
            title=f"[Auto-Triggered] {event.title}",
            objective=objective,
            domain=domain,
            priority=priority,
            execution_mode=self.default_mode,
            event_id=event.event_id,
        )
        logger.info(
            "proactive_ticket_spawned",
            event_id=event.event_id,
            ticket_id=ticket.ticket_id,
            mode=self.default_mode.value,
        )
        return ticket
