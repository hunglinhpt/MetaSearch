"""Data models for MetaSearch Autonomous Super-Agent Engine.

Inspired by DeepSeek Super-Agent Prototype (Router-Worker Decoupling,
Quiet Autonomy, Event-Driven Proactivity, Observe Mode, and Watchdog Supervisor).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


class TicketPriority(int, Enum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


class TicketStatus(StrEnum):
    PENDING = "pending"
    OBSERVED = "observed"  # In Observe/Shadow mode: ticket planned but not actively executed
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"  # Quiet Autonomy escalation: waiting for human
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    LIVE = "live"  # Actively runs full research & synthesis pipeline
    OBSERVE = "observe"  # Shadow evaluation: logs proposed actions without burning live quota


@dataclass(slots=True)
class SuperAgentEvent:
    """External or system event triggering autonomous action."""

    source: str  # "market_feed", "github_webhook", "sec_filing", "cve_monitor", "user_prompt"
    event_type: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:8]}")
    timestamp: float = field(default_factory=time.time)
    title: str = ""


@dataclass(slots=True)
class EscalationRequest:
    """Human-in-the-loop escalation under the Quiet Autonomy contract.

    The Super-Agent speaks only when a human must decide or a concrete result is ready.
    """

    escalation_id: str
    ticket_id: str
    reason: str
    proposed_options: list[str]
    context_summary: str
    created_at: float = field(default_factory=time.time)
    resolved: bool = False
    chosen_option: str | None = None
    resolved_by: str | None = None
    resolved_at: float | None = None


@dataclass(slots=True)
class SuperAgentArtifact:
    """Final actionable deliverable produced by an isolated Super-Agent Worker."""

    artifact_id: str
    artifact_type: str  # "research_report", "forensic_audit", "technical_signal", "action_plan"
    title: str
    content_md: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class SuperAgentTicket:
    """Autonomous work item dispatched by the Router to an isolated Worker."""

    ticket_id: str
    title: str
    objective: str
    domain: str  # "financial", "technology_ai", "biomedical_health", "general_discovery", etc.
    priority: TicketPriority = TicketPriority.NORMAL
    status: TicketStatus = TicketStatus.PENDING
    execution_mode: ExecutionMode = ExecutionMode.LIVE
    originating_event_id: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    assigned_worker_id: str | None = None
    step_history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[SuperAgentArtifact] = field(default_factory=list)
    escalations: list[EscalationRequest] = field(default_factory=list)
    error_message: str | None = None
    max_steps: int = 6
    current_step: int = 0
    token_budget: int = 16_000
    consumed_tokens: int = 0
    escalation_approved: bool = False


@dataclass(slots=True)
class WatchdogMetric:
    """Supervisory heartbeat and stability metrics recorded by Watchdog."""

    worker_id: str
    ticket_id: str
    last_heartbeat: float
    elapsed_ms: int
    step_count: int
    token_burn: int
    is_healthy: bool = True
    anomalies: list[str] = field(default_factory=list)
