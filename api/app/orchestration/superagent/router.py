"""Central Super-Agent Router (Router-Worker Decoupling).

Acts as the persistent global coordinator and single point of entry for tickets
and proactive events, decomposing complex objectives and dispatching them to
isolated Domain Workers.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.core.logging import get_logger
from app.orchestration.superagent.models import (
    EscalationRequest,
    ExecutionMode,
    SuperAgentEvent,
    SuperAgentTicket,
    TicketPriority,
    TicketStatus,
)
from app.orchestration.superagent.watchdog import WatchdogSupervisor
from app.orchestration.superagent.worker import IsolatedSuperAgentWorker
from app.retrieval.domain_intelligence import classify_domain_intelligence

logger = get_logger(__name__)


class SuperAgentRouter:
    """Coordinates tickets, dispatches to workers, and enforces the Router-Worker architecture."""

    def __init__(
        self,
        watchdog: WatchdogSupervisor | None = None,
        *,
        default_mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> None:
        self.watchdog = watchdog or WatchdogSupervisor()
        self.default_mode = default_mode
        self._tickets: dict[str, SuperAgentTicket] = {}
        self._events: list[SuperAgentEvent] = []

    def submit_goal(
        self,
        title: str,
        objective: str,
        *,
        domain: str = "auto",
        priority: TicketPriority = TicketPriority.NORMAL,
        execution_mode: ExecutionMode | None = None,
        event_id: str | None = None,
    ) -> SuperAgentTicket:
        """Create and enqueue a new Super-Agent ticket."""
        pack = classify_domain_intelligence(objective, requested_domain=domain)
        ticket_id = f"tkt_{uuid.uuid4().hex[:8]}"
        mode = execution_mode or self.default_mode

        # Automatic priority escalation if critical keywords detected
        obj_lower = objective.lower()
        if any(w in obj_lower for w in ("khẩn cấp", "critical", "sập sàn", "bctc mới nhất")):
            priority = TicketPriority.HIGH

        ticket = SuperAgentTicket(
            ticket_id=ticket_id,
            title=title,
            objective=objective,
            domain=pack.id,
            priority=priority,
            status=TicketStatus.PENDING,
            execution_mode=mode,
            originating_event_id=event_id,
            created_at=time.time(),
        )
        self._tickets[ticket_id] = ticket
        logger.info(
            "superagent_ticket_created",
            ticket_id=ticket_id,
            domain=pack.id,
            priority=priority.name,
            mode=mode.value,
        )
        return ticket

    async def dispatch_ticket(
        self,
        ticket_id: str,
        *,
        answer_pipeline: Any = None,
        council_engine: Any = None,
    ) -> SuperAgentTicket:
        """Assign ticket to a new isolated worker and execute."""
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise KeyError(f"Ticket {ticket_id} not found in router")

        worker = IsolatedSuperAgentWorker(watchdog=self.watchdog)
        return await worker.execute_ticket(
            ticket, answer_pipeline=answer_pipeline, council_engine=council_engine
        )

    def dispatch_background(
        self,
        ticket_id: str,
        *,
        answer_pipeline: Any = None,
        council_engine: Any = None,
    ) -> asyncio.Task[SuperAgentTicket]:
        """Fire-and-forget background execution of a ticket."""
        return asyncio.create_task(
            self.dispatch_ticket(
                ticket_id, answer_pipeline=answer_pipeline, council_engine=council_engine
            )
        )

    def resolve_escalation(
        self,
        ticket_id: str,
        escalation_id: str,
        chosen_option: str,
        resolved_by: str = "human_operator",
    ) -> SuperAgentTicket:
        """Resolve a human-in-the-loop escalation request and resume ticket processing."""
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise KeyError(f"Ticket {ticket_id} not found")

        target_esc: EscalationRequest | None = None
        for esc in ticket.escalations:
            if esc.escalation_id == escalation_id:
                target_esc = esc
                break

        if not target_esc:
            raise KeyError(f"Escalation {escalation_id} not found for ticket {ticket_id}")

        target_esc.resolved = True
        target_esc.chosen_option = chosen_option
        target_esc.resolved_by = resolved_by
        target_esc.resolved_at = time.time()

        if chosen_option.lower().startswith("abort"):
            ticket.status = TicketStatus.CANCELLED
            ticket.error_message = f"Cancelled via human escalation by {resolved_by}"
            ticket.finished_at = time.time()
        else:
            ticket.status = TicketStatus.PENDING

        logger.info(
            "superagent_escalation_resolved",
            ticket_id=ticket_id,
            escalation_id=escalation_id,
            chosen_option=chosen_option,
        )
        return ticket

    def list_tickets(
        self,
        *,
        status: TicketStatus | None = None,
        domain: str | None = None,
    ) -> list[SuperAgentTicket]:
        tickets = list(self._tickets.values())
        if status is not None:
            tickets = [t for t in tickets if t.status == status]
        if domain is not None:
            tickets = [t for t in tickets if t.domain == domain]
        return sorted(tickets, key=lambda t: (t.priority.value, -t.created_at))

    def get_ticket(self, ticket_id: str) -> SuperAgentTicket | None:
        return self._tickets.get(ticket_id)

    def register_event(self, event: SuperAgentEvent) -> None:
        self._events.append(event)
        if len(self._events) > 500:
            self._events = self._events[-500:]

    def list_events(self, limit: int = 50) -> list[SuperAgentEvent]:
        return list(reversed(self._events[-limit:]))
