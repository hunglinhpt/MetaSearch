"""MetaSearch Autonomous Super-Agent Engine (Router-Worker Architecture)."""

from __future__ import annotations

from app.orchestration.superagent.events import EventIngestionEngine
from app.orchestration.superagent.models import (
    EscalationRequest,
    ExecutionMode,
    SuperAgentArtifact,
    SuperAgentEvent,
    SuperAgentTicket,
    TicketPriority,
    TicketStatus,
    WatchdogMetric,
)
from app.orchestration.superagent.router import SuperAgentRouter
from app.orchestration.superagent.watchdog import WatchdogSupervisor
from app.orchestration.superagent.worker import IsolatedSuperAgentWorker

_global_watchdog = WatchdogSupervisor()
_global_router = SuperAgentRouter(watchdog=_global_watchdog)
_global_event_engine = EventIngestionEngine(router=_global_router)


def get_watchdog_supervisor() -> WatchdogSupervisor:
    return _global_watchdog


def get_superagent_router() -> SuperAgentRouter:
    return _global_router


def get_event_ingestion_engine() -> EventIngestionEngine:
    return _global_event_engine


__all__ = [
    "EscalationRequest",
    "EventIngestionEngine",
    "ExecutionMode",
    "IsolatedSuperAgentWorker",
    "SuperAgentArtifact",
    "SuperAgentEvent",
    "SuperAgentRouter",
    "SuperAgentTicket",
    "TicketPriority",
    "TicketStatus",
    "WatchdogMetric",
    "WatchdogSupervisor",
    "get_event_ingestion_engine",
    "get_superagent_router",
    "get_watchdog_supervisor",
]
