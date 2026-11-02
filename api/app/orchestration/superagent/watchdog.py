"""Watchdog Supervisor & Circuit Breaker for Autonomous Super-Agent Workers.

Prevents zombie workers, infinite ReAct loops, token burns, and cascading failures.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logging import get_logger
from app.orchestration.superagent.models import SuperAgentTicket, TicketStatus, WatchdogMetric

logger = get_logger(__name__)


class WatchdogSupervisor:
    """Monitors running Super-Agent workers, enforces circuit-breakers, and handles recovery."""

    def __init__(
        self,
        *,
        max_duration_sec: float = 120.0,
        max_token_budget: int = 32_000,
        heartbeat_timeout_sec: float = 45.0,
    ) -> None:
        self.max_duration_sec = max_duration_sec
        self.max_token_budget = max_token_budget
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self._active_metrics: dict[str, WatchdogMetric] = {}
        self._tripped_circuits: list[dict[str, Any]] = []

    def register_worker(self, worker_id: str, ticket: SuperAgentTicket) -> None:
        self._active_metrics[worker_id] = WatchdogMetric(
            worker_id=worker_id,
            ticket_id=ticket.ticket_id,
            last_heartbeat=time.time(),
            elapsed_ms=0,
            step_count=0,
            token_burn=0,
            is_healthy=True,
        )

    def record_heartbeat(
        self,
        worker_id: str,
        *,
        step: int,
        tokens_added: int = 0,
    ) -> None:
        metric = self._active_metrics.get(worker_id)
        if not metric:
            return
        now = time.time()
        metric.elapsed_ms = int((now - metric.last_heartbeat) * 1000)
        metric.last_heartbeat = now
        metric.step_count = step
        metric.token_burn += tokens_added

    def unregister_worker(self, worker_id: str) -> None:
        self._active_metrics.pop(worker_id, None)

    def audit_ticket_health(
        self, worker_id: str, ticket: SuperAgentTicket
    ) -> tuple[bool, list[str]]:
        """Evaluate whether a worker is exhibiting abnormal behavior or stuck execution."""
        now = time.time()
        metric = self._active_metrics.get(worker_id)
        anomalies: list[str] = []

        if ticket.started_at:
            duration = now - ticket.started_at
            if duration > self.max_duration_sec:
                anomalies.append(
                    f"EXCESSIVE_DURATION: elapsed {duration:.1f}s > limit {self.max_duration_sec}s"
                )

        if ticket.consumed_tokens > self.max_token_budget:
            anomalies.append(
                f"TOKEN_BURST: consumed {ticket.consumed_tokens} > limit {self.max_token_budget}"
            )

        if metric and (now - metric.last_heartbeat) > self.heartbeat_timeout_sec:
            anomalies.append(
                f"HEARTBEAT_TIMEOUT: no activity for {now - metric.last_heartbeat:.1f}s"
            )

        if ticket.current_step > ticket.max_steps:
            anomalies.append(
                f"MAX_STEPS_EXCEEDED: step {ticket.current_step} exceeds max {ticket.max_steps}"
            )

        is_healthy = len(anomalies) == 0
        if metric:
            metric.is_healthy = is_healthy
            metric.anomalies = anomalies

        if not is_healthy:
            logger.warning(
                "watchdog_circuit_breaker_triggered",
                worker_id=worker_id,
                ticket_id=ticket.ticket_id,
                anomalies=anomalies,
            )
            self._tripped_circuits.append(
                {
                    "worker_id": worker_id,
                    "ticket_id": ticket.ticket_id,
                    "timestamp": now,
                    "anomalies": anomalies,
                }
            )
            ticket.status = TicketStatus.FAILED
            ticket.error_message = f"Watchdog Circuit Breaker Tripped: {'; '.join(anomalies)}"

        return is_healthy, anomalies

    def snapshot(self) -> dict[str, Any]:
        return {
            "monitored_workers_count": len(self._active_metrics),
            "max_duration_sec": self.max_duration_sec,
            "max_token_budget": self.max_token_budget,
            "active_metrics": [
                {
                    "worker_id": m.worker_id,
                    "ticket_id": m.ticket_id,
                    "elapsed_ms": m.elapsed_ms,
                    "step_count": m.step_count,
                    "token_burn": m.token_burn,
                    "is_healthy": m.is_healthy,
                    "anomalies": m.anomalies,
                }
                for m in self._active_metrics.values()
            ],
            "tripped_circuits": self._tripped_circuits[-20:],
        }
