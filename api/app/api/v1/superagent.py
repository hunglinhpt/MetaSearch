"""REST API for MetaSearch Autonomous Super-Agent Engine.

Implements DeepSeek Super-agent architecture: Router-Worker decoupling,
Observe mode, Quiet Autonomy escalations, Proactive Event Ingestion,
and Watchdog Circuit Breaker supervision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api import deps
from app.core.auth import require_admin
from app.orchestration.superagent import (
    ExecutionMode,
    SuperAgentTicket,
    TicketPriority,
    TicketStatus,
    get_event_ingestion_engine,
    get_superagent_router,
    get_watchdog_supervisor,
)

router = APIRouter(prefix="/superagent", tags=["superagent"])


class TicketCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)
    domain: str = Field(default="auto", max_length=50)
    priority: int = Field(default=3, ge=1, le=4)
    execution_mode: str = Field(default="live", pattern="^(live|observe)$")
    run_immediately: bool = True


class EscalationResolveRequest(BaseModel):
    chosen_option: str = Field(min_length=1, max_length=500)
    resolved_by: str = Field(default="human_operator", max_length=100)


class EventIngestRequest(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(default="", max_length=200)
    auto_spawn_ticket: bool = True


def _serialize_ticket(t: SuperAgentTicket) -> dict[str, Any]:
    return {
        "ticket_id": t.ticket_id,
        "title": t.title,
        "objective": t.objective,
        "domain": t.domain,
        "priority": t.priority.value,
        "priority_name": t.priority.name,
        "status": t.status.value,
        "execution_mode": t.execution_mode.value,
        "originating_event_id": t.originating_event_id,
        "created_at": t.created_at,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "assigned_worker_id": t.assigned_worker_id,
        "step_history": t.step_history,
        "current_step": t.current_step,
        "max_steps": t.max_steps,
        "consumed_tokens": t.consumed_tokens,
        "error_message": t.error_message,
        "artifacts": [
            {
                "artifact_id": a.artifact_id,
                "artifact_type": a.artifact_type,
                "title": a.title,
                "content_md": a.content_md,
                "citations": a.citations,
                "metadata": a.metadata,
                "created_at": a.created_at,
            }
            for a in t.artifacts
        ],
        "escalations": [
            {
                "escalation_id": e.escalation_id,
                "ticket_id": e.ticket_id,
                "reason": e.reason,
                "proposed_options": e.proposed_options,
                "context_summary": e.context_summary,
                "created_at": e.created_at,
                "resolved": e.resolved,
                "chosen_option": e.chosen_option,
                "resolved_by": e.resolved_by,
                "resolved_at": e.resolved_at,
            }
            for e in t.escalations
        ],
    }


@router.post("/tickets", summary="Submit a high-level goal to the Super-Agent Router")
async def create_ticket(request: TicketCreateRequest) -> dict[str, Any]:
    router_inst = get_superagent_router()
    prio = TicketPriority(request.priority)
    mode = ExecutionMode(request.execution_mode)

    ticket = router_inst.submit_goal(
        title=request.title,
        objective=request.objective,
        domain=request.domain,
        priority=prio,
        execution_mode=mode,
    )

    if request.run_immediately:
        router_inst.dispatch_background(
            ticket.ticket_id,
            answer_pipeline=deps.get_answer_pipeline(),
            council_engine=None,
        )

    return {"ok": True, "ticket": _serialize_ticket(ticket)}


@router.get("/tickets", summary="List all Super-Agent tickets with optional filtering")
async def list_tickets(
    status: str | None = Query(default=None),
    domain: str | None = Query(default=None),
) -> dict[str, Any]:
    router_inst = get_superagent_router()
    st_filter = TicketStatus(status) if status else None
    tickets = router_inst.list_tickets(status=st_filter, domain=domain)
    return {
        "total_tickets": len(tickets),
        "tickets": [_serialize_ticket(t) for t in tickets],
    }


@router.get("/tickets/{ticket_id}", summary="Get execution trace and artifacts of a ticket")
async def get_ticket(ticket_id: str) -> dict[str, Any]:
    router_inst = get_superagent_router()
    ticket = router_inst.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket '{ticket_id}' not found")
    return {"ok": True, "ticket": _serialize_ticket(ticket)}


@router.post("/tickets/{ticket_id}/dispatch", summary="Manually trigger worker for a ticket")
async def dispatch_ticket_endpoint(ticket_id: str) -> dict[str, Any]:
    router_inst = get_superagent_router()
    ticket = router_inst.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket '{ticket_id}' not found")

    updated = await router_inst.dispatch_ticket(
        ticket_id,
        answer_pipeline=deps.get_answer_pipeline(),
        council_engine=None,
    )
    return {"ok": True, "ticket": _serialize_ticket(updated)}


@router.post(
    "/tickets/{ticket_id}/escalate/{escalation_id}/resolve",
    summary="Resolve a human-in-the-loop Quiet Autonomy escalation request",
    dependencies=[Depends(require_admin)],
)
async def resolve_escalation_endpoint(
    ticket_id: str,
    escalation_id: str,
    request: EscalationResolveRequest,
) -> dict[str, Any]:
    router_inst = get_superagent_router()
    try:
        updated = router_inst.resolve_escalation(
            ticket_id,
            escalation_id,
            chosen_option=request.chosen_option,
            resolved_by=request.resolved_by,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"ok": True, "ticket": _serialize_ticket(updated)}


@router.post("/events/ingest", summary="Ingest proactive real-time event and trigger ticket")
async def ingest_event_endpoint(request: EventIngestRequest) -> dict[str, Any]:
    engine = get_event_ingestion_engine()
    event, ticket = engine.ingest(
        source=request.source,
        event_type=request.event_type,
        payload=request.payload,
        title=request.title,
        auto_spawn_ticket=request.auto_spawn_ticket,
    )
    return {
        "ok": True,
        "event_id": event.event_id,
        "source": event.source,
        "event_type": event.event_type,
        "ticket": _serialize_ticket(ticket) if ticket else None,
    }


@router.get("/events", summary="List recent proactive events")
async def list_events_endpoint(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    events = get_superagent_router().list_events(limit=limit)
    return {
        "total_events": len(events),
        "events": [
            {
                "event_id": e.event_id,
                "source": e.source,
                "event_type": e.event_type,
                "title": e.title,
                "payload": e.payload,
                "timestamp": e.timestamp,
            }
            for e in events
        ],
    }


@router.get("/watchdog/status", summary="Inspect Watchdog Supervisor status and tripped circuits")
async def get_watchdog_status() -> dict[str, Any]:
    return get_watchdog_supervisor().snapshot()
