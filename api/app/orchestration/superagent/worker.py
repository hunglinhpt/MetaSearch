"""Isolated Autonomous Worker implementing DeepSeek's Quiet Autonomy protocol.

Operates in an isolated session, executes multi-step ReAct reasoning with
domain intelligence, and produces actionable deliverables without spamming the user.

Strict Data Integrity (WP-10 / DAT-05):
- Zero fabricated deliverable artifacts ("Verified.") when LLM/council is missing.
- Exact token consumption accounting from engine usage, removing fake constants.
- Watchdog timeout execution via asyncio.timeout.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.core.logging import get_logger
from app.extraction.chunker import Chunk
from app.orchestration.superagent.models import (
    EscalationRequest,
    ExecutionMode,
    SuperAgentArtifact,
    SuperAgentTicket,
    TicketStatus,
)
from app.orchestration.superagent.watchdog import WatchdogSupervisor
from app.retrieval.domain_intelligence import (
    classify_domain_intelligence,
)

logger = get_logger(__name__)


class IsolatedSuperAgentWorker:
    """Isolated session worker executing an assigned Super-Agent ticket."""

    def __init__(
        self,
        worker_id: str | None = None,
        watchdog: WatchdogSupervisor | None = None,
    ) -> None:
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.watchdog = watchdog or WatchdogSupervisor()

    async def execute_ticket(
        self,
        ticket: SuperAgentTicket,
        *,
        answer_pipeline: Any = None,
        council_engine: Any = None,
    ) -> SuperAgentTicket:
        """Execute ticket in an isolated session with watchdog monitoring."""
        ticket.started_at = time.time()
        ticket.assigned_worker_id = self.worker_id
        ticket.status = TicketStatus.RUNNING

        self.watchdog.register_worker(self.worker_id, ticket)
        logger.info(
            "superagent_worker_started",
            worker_id=self.worker_id,
            ticket_id=ticket.ticket_id,
            domain=ticket.domain,
            mode=ticket.execution_mode.value,
        )

        try:
            async with asyncio.timeout(self.watchdog.max_duration_sec):
                # 1. Observe / Shadow Mode: Perform zero-burn evaluation without active execution
                if ticket.execution_mode == ExecutionMode.OBSERVE:
                    await self._execute_observe_mode(ticket)
                    return ticket

                # 2. Live Execution: Multi-step ReAct under Quiet Autonomy
                await self._execute_live_mode(
                    ticket, answer_pipeline=answer_pipeline, council_engine=council_engine
                )
                return ticket
        except TimeoutError:
            ticket.status = TicketStatus.FAILED
            ticket.finished_at = time.time()
            ticket.error_message = f"Watchdog timeout exceeded: {self.watchdog.max_duration_sec}s"
            self.watchdog.audit_ticket_health(self.worker_id, ticket)
            logger.warning(
                "superagent_worker_timed_out",
                worker_id=self.worker_id,
                ticket_id=ticket.ticket_id,
            )
            return ticket
        except Exception as exc:
            ticket.status = TicketStatus.FAILED
            ticket.error_message = str(exc)[:400]
            ticket.finished_at = time.time()
            logger.error(
                "superagent_worker_failed",
                worker_id=self.worker_id,
                ticket_id=ticket.ticket_id,
                error=str(exc),
            )
            return ticket
        finally:
            self.watchdog.unregister_worker(self.worker_id)

    async def _execute_observe_mode(self, ticket: SuperAgentTicket) -> None:
        """Shadow evaluation: Formulates plan without burning live API budget."""
        pack = classify_domain_intelligence(ticket.objective, requested_domain=ticket.domain)
        ticket.current_step = 1
        ticket.step_history.append(
            {
                "step": 1,
                "action": "shadow_domain_classification",
                "result": f"Classified into {pack.name} ({pack.id}), maturity: {pack.maturity}",
                "timestamp": time.time(),
            }
        )

        plan = [
            f"Query targeted engines: {', '.join(pack.preferred_engines[:4])}",
            f"Decompose along dimensions: {'; '.join(pack.essence_dimensions[:3])}",
            f"Audit against rubric: {'; '.join(pack.verification_rubric[:2])}",
        ]
        ticket.step_history.append(
            {
                "step": 2,
                "action": "shadow_action_plan_formulation",
                "result": plan,
                "timestamp": time.time(),
            }
        )

        ticket.artifacts.append(
            SuperAgentArtifact(
                artifact_id=f"art_shadow_{uuid.uuid4().hex[:6]}",
                artifact_type="action_plan",
                title=f"[Shadow Plan] {ticket.title}",
                content_md=(
                    f"### Super-Agent Observe Mode Evaluation for: {ticket.title}\n\n"
                    f"**Detected Domain**: `{pack.id}` ({pack.name})\n"
                    f"**Proposed Engines**: {', '.join(pack.preferred_engines)}\n\n"
                    "#### Actionable Plan (Awaiting Live Trigger):\n"
                    + "\n".join(f"- {step}" for step in plan)
                ),
                metadata={"shadow_mode": True, "domain": pack.id},
            )
        )
        ticket.status = TicketStatus.OBSERVED
        ticket.finished_at = time.time()

    async def _execute_live_mode(
        self,
        ticket: SuperAgentTicket,
        answer_pipeline: Any,
        council_engine: Any,
    ) -> None:
        pack = classify_domain_intelligence(ticket.objective, requested_domain=ticket.domain)
        ticket.current_step = 1
        self.watchdog.record_heartbeat(self.worker_id, step=1, tokens_added=0)

        # Step 1: Query Context Preparation & Search
        ticket.step_history.append(
            {
                "step": 1,
                "action": "gather_evidence",
                "domain": pack.id,
                "timestamp": time.time(),
            }
        )

        chunks: list[tuple[int, Chunk]] = []
        if answer_pipeline is not None and hasattr(answer_pipeline, "prepare_context"):
            from app.orchestration.pipeline import SearchParams

            params = SearchParams(query=ticket.objective, k=5, categories=pack.categories)
            ctx = await answer_pipeline.prepare_context(params, domain=pack.id, budget=8000)
            raw_chunks = list(ctx.get("chunks") or [])
            for c in raw_chunks:
                num = int(c.get("n", 1))
                chunk = Chunk(
                    id=str(c.get("chunk_id") or f"chunk-{num}"),
                    document_url=str(c.get("url") or "https://example.com"),
                    source_title=str(c.get("title") or c.get("url") or "Evidence"),
                    heading_path="",
                    position=num,
                    content=str(c.get("content") or ""),
                    word_count=len(str(c.get("content") or "").split()),
                )
                chunks.append((num, chunk))

        ticket.current_step = 2
        self.watchdog.record_heartbeat(self.worker_id, step=2, tokens_added=0)

        # Step 2: Anomaly / Escalation Check (Quiet Autonomy Contract)
        obj_lower = ticket.objective.lower()
        is_high_risk = any(
            w in obj_lower
            for w in (
                "gian lận nghiêm trọng",
                "khởi tố",
                "thao túng báo cáo tài chính",
                "thao túng bctc",
            )
        )

        if is_high_risk and not ticket.escalation_approved:
            ticket.escalations.append(
                EscalationRequest(
                    escalation_id=f"esc_{uuid.uuid4().hex[:6]}",
                    ticket_id=ticket.ticket_id,
                    reason=(
                        "High-risk legal / fraud investigation requires human review confirmation."
                    ),
                    proposed_options=[
                        "Proceed with forensic audit in strictly read-only mode",
                        "Abort ticket",
                    ],
                    context_summary=(
                        f"Detected high-risk legal trigger in objective: {ticket.objective}"
                    ),
                )
            )
            ticket.status = TicketStatus.WAITING_HUMAN
            ticket.step_history.append(
                {
                    "step": 2,
                    "action": "quiet_autonomy_escalate",
                    "reason": "High-risk trigger",
                    "timestamp": time.time(),
                }
            )
            return

        # Step 3: Synthesis & Verification
        ticket.current_step = 3
        answer_text = ""
        citations_list: list[dict[str, Any]] = []

        if council_engine is not None and hasattr(council_engine, "run_council"):
            council_res = await council_engine.run_council(
                ticket.objective, chunks, domain=pack.id, enable_cross_review=True
            )
            answer_text = council_res.get("verdict_md") or council_res.get("answer_md") or ""
            citations_list = list(council_res.get("citations") or [])
            usage = council_res.get("usage", {})
            tokens_used = int(usage.get("total_tokens") or usage.get("totalTokens") or 0)
            ticket.consumed_tokens += tokens_used
            self.watchdog.record_heartbeat(self.worker_id, step=3, tokens_added=tokens_used)
        else:
            # Strict Data Integrity (DAT-05): Do NOT fabricate deliverables when LLM is missing!
            ticket.status = TicketStatus.FAILED
            ticket.error_message = "no_llm_configured"
            ticket.finished_at = time.time()
            ticket.step_history.append(
                {
                    "step": 3,
                    "action": "llm_synthesis_failed",
                    "error": "no_llm_configured",
                    "timestamp": time.time(),
                }
            )
            return

        # Step 4: Deliverable Packaging (Quiet Autonomy Contract)
        ticket.artifacts.append(
            SuperAgentArtifact(
                artifact_id=f"art_{uuid.uuid4().hex[:6]}",
                artifact_type="research_report",
                title=ticket.title,
                content_md=answer_text,
                citations=citations_list,
                metadata={"domain": pack.id, "worker_id": self.worker_id},
            )
        )
        ticket.status = TicketStatus.COMPLETED
        ticket.finished_at = time.time()
        ticket.step_history.append(
            {
                "step": 4,
                "action": "deliverable_packaged",
                "citations_count": len(citations_list),
                "timestamp": time.time(),
            }
        )
        logger.info(
            "superagent_ticket_completed",
            worker_id=self.worker_id,
            ticket_id=ticket.ticket_id,
            artifacts=len(ticket.artifacts),
            consumed_tokens=ticket.consumed_tokens,
        )
