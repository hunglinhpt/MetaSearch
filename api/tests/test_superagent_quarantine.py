"""Tests for SuperAgent Quarantine, State Machine & Anti-Hallucination (WP-10).

Enforces:
- DAT-05 / P10: Zero fabricated artifacts ("Verified.") when LLM is missing; status=FAILED.
- DAT-05: Real token accounting from engine usage, removing hardcoded 4200 tokens.
- REL-11 / P11: Proper state machine transitions and escalation loop resolution.
- Memory bounding: Ticket store bounded to 1,000 LRU entries with task references.
- Config isolation: superagent_enabled=False returns 404 and hides from OpenAPI.
- Error handling: Invalid status yields 400 Bad Request via MetaSearchError.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import app
from app.orchestration.superagent.models import (
    SuperAgentTicket,
    TicketStatus,
)
from app.orchestration.superagent.router import SuperAgentRouter
from app.orchestration.superagent.watchdog import WatchdogSupervisor
from app.orchestration.superagent.worker import IsolatedSuperAgentWorker


class TestSuperAgentQuarantine:
    def test_superagent_disabled_returns_404(self, monkeypatch):
        """When superagent_enabled is False, all /v1/superagent/* routes return 404."""
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "superagent_enabled", False)

        client = TestClient(app)
        resp = client.get("/v1/superagent/tickets")
        assert resp.status_code == 404
        data = resp.json()
        assert "quarantined" in str(data).lower() or "disabled" in str(data).lower()

    async def test_no_fabricated_artifact_without_llm_p10(self):
        """P10 / DAT-05: When no LLM/council is configured, worker MUST fail

        without creating any fake 'Verified.' artifact.
        """
        router = SuperAgentRouter()
        ticket = router.submit_goal(
            title="Kiểm toán FPT",
            objective="Phân tích báo cáo tài chính FPT 2024",
        )

        # Dispatch with council_engine=None and answer_pipeline=None
        worker = IsolatedSuperAgentWorker(watchdog=router.watchdog)
        updated = await worker.execute_ticket(ticket, council_engine=None, answer_pipeline=None)

        assert updated.status == TicketStatus.FAILED
        assert updated.error_message == "no_llm_configured"
        assert len(updated.artifacts) == 0, "MUST NOT create fabricated artifacts!"
        assert updated.consumed_tokens == 0, "MUST NOT add fake 4200 tokens!"

    async def test_tokens_come_from_usage(self):
        """Token consumption must be extracted from real council engine usage."""
        router = SuperAgentRouter()
        ticket = router.submit_goal(
            title="Real Token Audit",
            objective="Tính toán số token tiêu thụ thực tế",
        )

        mock_council = AsyncMock()
        mock_council.run_council.return_value = {
            "verdict_md": "## Real Analysis Verdict\n\nVerified factual content.",
            "citations": [],
            "usage": {"total_tokens": 580},
        }

        worker = IsolatedSuperAgentWorker(watchdog=router.watchdog)
        updated = await worker.execute_ticket(ticket, council_engine=mock_council)

        assert updated.status == TicketStatus.COMPLETED
        assert len(updated.artifacts) == 1
        assert updated.consumed_tokens == 580, f"Expected 580 tokens, got {updated.consumed_tokens}"

    async def test_watchdog_cancels_overrunning_ticket(self):
        """Watchdog supervisor must enforce max_duration_sec and cancel overrunning ticket."""
        watchdog = WatchdogSupervisor(max_duration_sec=0.05)
        worker = IsolatedSuperAgentWorker(watchdog=watchdog)

        ticket = SuperAgentTicket(
            ticket_id="tkt_slow",
            title="Slow ticket",
            objective="Slow running ticket exceeding timeout",
            domain="technology_ai",
        )

        async def slow_council(*args, **kwargs):
            await asyncio.sleep(0.2)
            return {"verdict_md": "too late", "citations": []}

        mock_council = AsyncMock()
        mock_council.run_council.side_effect = slow_council

        updated = await worker.execute_ticket(ticket, council_engine=mock_council)
        assert updated.status == TicketStatus.FAILED
        err_msg = (updated.error_message or "").lower()
        assert "timeout" in err_msg or "duration" in err_msg

    async def test_escalation_resume_completes_p11(self):
        """P11 / REL-11: Resolving escalation with approval must allow worker

        to resume and complete without re-triggering the escalation loop.
        """
        router = SuperAgentRouter()
        # Query with sensitive keyword triggering Quiet Autonomy escalation
        ticket = router.submit_goal(
            title="Pháp y nhạy cảm",
            objective="Điều tra dấu hiệu thao túng báo cáo tài chính và gian lận",
        )

        worker = IsolatedSuperAgentWorker(watchdog=router.watchdog)
        # 1st run: must pause and create escalation
        ticket_p1 = await worker.execute_ticket(ticket, council_engine=None)
        assert ticket_p1.status == TicketStatus.WAITING_HUMAN
        assert len(ticket_p1.escalations) == 1
        esc_id = ticket_p1.escalations[0].escalation_id

        # Human operator resolves escalation with approval
        router.resolve_escalation(
            ticket.ticket_id,
            esc_id,
            chosen_option="Tiếp tục phân tích chuyên sâu (Approved)",
        )
        assert ticket.status == TicketStatus.PENDING

        # Mock council for 2nd run
        mock_council = AsyncMock()
        mock_council.run_council.return_value = {
            "verdict_md": "## Phân tích pháp y sau phê duyệt",
            "citations": [],
            "usage": {"total_tokens": 400},
        }

        # 2nd run: MUST NOT re-escalate (escalations remains 1), should complete!
        ticket_p2 = await worker.execute_ticket(ticket, council_engine=mock_council)
        assert ticket_p2.status == TicketStatus.COMPLETED
        assert len(ticket_p2.escalations) == 1, "Must not create duplicate escalations!"
        assert len(ticket_p2.artifacts) == 1

    def test_ticket_store_bounded(self):
        """Router _tickets store must be bounded to 1,000 entries (LRU)."""
        router = SuperAgentRouter()
        for i in range(1100):
            router.submit_goal(title=f"Goal {i}", objective=f"Objective {i}")

        assert len(router._tickets) <= 1000, f"Ticket store leaked! Count: {len(router._tickets)}"

    def test_invalid_status_is_400(self, monkeypatch):
        """Listing tickets with an invalid status query parameter must return 400, not 500."""
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "superagent_enabled", True)

        client = TestClient(app)
        resp = client.get("/v1/superagent/tickets?status=bogus_status_name")
        assert resp.status_code == 400
        data = resp.json()
        assert "invalid" in str(data).lower()
