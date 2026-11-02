"""Comprehensive tests for MetaSearch Autonomous Super-Agent Engine.

Covers:
1. Router-Worker decoupling and isolated ticket dispatch.
2. DeepSeek's "Quiet Autonomy" protocol and human escalations.
3. Observe (Shadow) Mode zero-burn evaluation.
4. Event-Driven Proactive Ingestion (Financial, Security, Biomedical).
5. Watchdog Supervisor and Circuit Breaker enforcement.
6. REST API endpoints (/v1/superagent/*).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orchestration.superagent import (
    EventIngestionEngine,
    ExecutionMode,
    IsolatedSuperAgentWorker,
    SuperAgentRouter,
    SuperAgentTicket,
    TicketPriority,
    TicketStatus,
    WatchdogSupervisor,
)


class DummyAnswerPipeline:
    """Mock answer pipeline providing synthetic context chunks for isolated testing."""

    async def prepare_context(
        self, params: Any, domain: str = "auto", budget: int = 8000
    ) -> dict[str, Any]:
        return {
            "chunks": [
                {
                    "n": 1,
                    "chunk_id": "chunk-vcb-1",
                    "url": "https://vietstock.vn/vcb-bctc-2024",
                    "title": "BCTC Q4/2024 VCB",
                    "content": (
                        "NPL tỷ lệ nợ xấu VCB ở mức 0.98%, trích lập LLR đạt 230%, CAR 11.5%."
                    ),
                },
                {
                    "n": 2,
                    "chunk_id": "chunk-fpt-2",
                    "url": "https://fpt.com.vn/ir-2024",
                    "title": "FPT Doanh thu AI 2024",
                    "content": (
                        "Doanh thu dịch vụ CNTT tăng 28%, AI factory mở rộng hợp tác quốc tế."
                    ),
                },
            ],
            "sources": [{"url": "https://vietstock.vn/vcb", "title": "VCB"}],
        }


class TestSuperAgentRouterAndWorker:
    @pytest.mark.asyncio
    async def test_submit_and_dispatch_ticket_live_mode(self) -> None:
        watchdog = WatchdogSupervisor()
        router = SuperAgentRouter(watchdog=watchdog)
        pipeline = DummyAnswerPipeline()

        ticket = router.submit_goal(
            title="Phân tích pháp y BCTC VCB 2024",
            objective="Bóc tách CAMELS nợ xấu NPL và trích lập dự phòng VCB",
            domain="financial",
            priority=TicketPriority.NORMAL,
            execution_mode=ExecutionMode.LIVE,
        )
        assert ticket.status == TicketStatus.PENDING
        assert ticket.domain == "financial"

        updated = await router.dispatch_ticket(ticket.ticket_id, answer_pipeline=pipeline)
        assert updated.status == TicketStatus.COMPLETED
        assert len(updated.artifacts) >= 1
        artifact = updated.artifacts[0]
        assert artifact.artifact_type == "research_report"
        assert "[1]" in artifact.content_md
        assert len(artifact.citations) >= 1
        assert updated.assigned_worker_id is not None
        assert updated.finished_at is not None

    @pytest.mark.asyncio
    async def test_observe_shadow_mode_evaluation(self) -> None:
        watchdog = WatchdogSupervisor()
        router = SuperAgentRouter(watchdog=watchdog)

        ticket = router.submit_goal(
            title="Đánh giá kiến trúc HNSW vs IVFFlat",
            objective="So sánh bộ nhớ và recall của pgvector HNSW và IVFFlat",
            domain="technology_ai",
            execution_mode=ExecutionMode.OBSERVE,
        )

        updated = await router.dispatch_ticket(ticket.ticket_id)
        assert updated.status == TicketStatus.OBSERVED
        assert len(updated.artifacts) == 1
        art = updated.artifacts[0]
        assert art.artifact_type == "action_plan"
        assert "Observe Mode" in art.content_md
        assert art.metadata.get("shadow_mode") is True


class TestQuietAutonomyAndEscalation:
    @pytest.mark.asyncio
    async def test_high_risk_triggers_human_escalation(self) -> None:
        watchdog = WatchdogSupervisor()
        router = SuperAgentRouter(watchdog=watchdog)

        # Trigger keyword indicating extreme high risk
        ticket = router.submit_goal(
            title="Điều tra gian lận cổ phiếu",
            objective="Dấu hiệu gian lận nghiêm trọng và nguy cơ khởi tố tại công ty X",
            domain="financial",
            execution_mode=ExecutionMode.LIVE,
        )

        updated = await router.dispatch_ticket(ticket.ticket_id)
        assert updated.status == TicketStatus.WAITING_HUMAN
        assert len(updated.escalations) == 1
        esc = updated.escalations[0]
        assert esc.resolved is False
        assert len(esc.proposed_options) >= 2

        # Human operator resolves the escalation
        resolved = router.resolve_escalation(
            ticket.ticket_id,
            esc.escalation_id,
            chosen_option="Proceed with forensic audit in strictly read-only mode",
            resolved_by="lead_compliance_officer",
        )
        assert resolved.status == TicketStatus.PENDING
        assert resolved.escalations[0].resolved is True
        assert resolved.escalations[0].resolved_by == "lead_compliance_officer"


class TestProactiveEventIngestion:
    def test_ingest_market_filing_event_spawns_high_priority_ticket(self) -> None:
        router = SuperAgentRouter()
        engine = EventIngestionEngine(router, default_mode=ExecutionMode.OBSERVE)

        event, ticket = engine.ingest(
            source="viet_stock_announcement",
            event_type="bctc_audited_filing",
            payload={
                "ticker": "HPG",
                "summary": "BCTC kiểm toán năm 2024 công bố doanh thu kỷ lục",
            },
            title="HPG Audited Filing 2024",
        )
        assert event.source == "viet_stock_announcement"
        assert ticket is not None
        assert ticket.domain == "financial"
        assert ticket.priority == TicketPriority.HIGH
        assert ticket.execution_mode == ExecutionMode.OBSERVE
        assert "HPG" in ticket.objective

    def test_ingest_cve_security_event_spawns_tech_ticket(self) -> None:
        router = SuperAgentRouter()
        engine = EventIngestionEngine(router)

        _event, ticket = engine.ingest(
            source="cve_monitor",
            event_type="security_vulnerability",
            payload={"repo": "fastapi-gateway", "details": "Buffer vulnerability CVE-2026-9999"},
            title="CVE-2026-9999 Critical Notice",
        )
        assert ticket is not None
        assert ticket.domain == "technology_ai"
        assert ticket.priority == TicketPriority.HIGH


class TestWatchdogSupervisorCircuitBreaker:
    def test_watchdog_detects_token_burst_and_trips(self) -> None:
        watchdog = WatchdogSupervisor(max_token_budget=10_000)
        worker = IsolatedSuperAgentWorker(watchdog=watchdog)
        ticket = SuperAgentTicket(
            ticket_id="tkt_test_burst",
            title="Test Burst",
            objective="Testing watchdog",
            domain="general_discovery",
            consumed_tokens=15_000,  # Exceeds 10,000 limit
        )
        watchdog.register_worker(worker.worker_id, ticket)
        is_healthy, anomalies = watchdog.audit_ticket_health(worker.worker_id, ticket)
        assert is_healthy is False
        assert any("TOKEN_BURST" in a for a in anomalies)
        assert ticket.status == TicketStatus.FAILED
        assert "Watchdog Circuit Breaker Tripped" in (ticket.error_message or "")


class TestSuperAgentRestApiEndpoints:
    def test_create_and_list_tickets_endpoint(self) -> None:
        client = TestClient(app)
        res = client.post(
            "/v1/superagent/tickets",
            json={
                "title": "API Test Goal",
                "objective": "Test objective for SuperAgent",
                "domain": "general_discovery",
                "priority": 3,
                "execution_mode": "observe",
                "run_immediately": False,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        tkt = data["ticket"]
        tkt_id = tkt["ticket_id"]
        assert tkt["status"] == "pending"

        # List tickets
        res_list = client.get("/v1/superagent/tickets")
        assert res_list.status_code == 200
        list_data = res_list.json()
        assert list_data["total_tickets"] >= 1
        ids = [item["ticket_id"] for item in list_data["tickets"]]
        assert tkt_id in ids

        # Get ticket details
        res_get = client.get(f"/v1/superagent/tickets/{tkt_id}")
        assert res_get.status_code == 200
        assert res_get.json()["ticket"]["ticket_id"] == tkt_id

    def test_event_ingest_and_watchdog_status_endpoints(self) -> None:
        client = TestClient(app)
        res = client.post(
            "/v1/superagent/events/ingest",
            json={
                "source": "api_test_source",
                "event_type": "market_pulse",
                "payload": {"ticker": "VCB", "text": "Tin tức thị trường VCB"},
                "title": "VCB Pulse",
                "auto_spawn_ticket": True,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["ticket"] is not None

        # Watchdog status
        res_wd = client.get("/v1/superagent/watchdog/status")
        assert res_wd.status_code == 200
        wd_data = res_wd.json()
        assert "monitored_workers_count" in wd_data
        assert "max_duration_sec" in wd_data
