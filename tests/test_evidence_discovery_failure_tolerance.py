"""Network-failure-injection / fail-open tolerance tests for
`evidence_discovery_service.descubrir_evidencias` — phase 4.3 of
radicacion-sin-friccion (openspec/changes/radicacion-sin-friccion/tasks.md
~L222).

`descubrir_evidencias` orchestrates Gmail/Outlook + Drive + Calendar behind a
"fail-open per provider, fail-closed only when EVERY provider is
disconnected" contract:

- No provider connected at all -> `ExternalServiceError` with
  `code=NO_PROVIDER_CONNECTED` (502 via `domain_to_http`).
- One connected provider's email search raising -> that provider's evidence
  is simply empty; the request still succeeds (200/partial results).
- Drive raising -> email/calendar evidence for the SAME request is
  unaffected (per-source try/except in `descubrir_evidencias`).
- A single failing email query inside `_gather_email_evidence` does not
  drop the results of the OTHER queries in the same batch.

To isolate this orchestration-level contract from the downstream LLM-backed
matcher/justify pipeline (out of scope here — see
test_litellm_adapter_failures.py for that seam), the five agent-graph nodes
downstream of the provider-gather step are stubbed as pass-throughs that
leave `state` untouched. This is a legitimate module-level seam:
`evidence_discovery_service` imports each node by name.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import AgentState
from app.core.exceptions import NO_PROVIDER_CONNECTED, ExternalServiceError
from app.models.integracion import IntegrationProvider
from app.schemas.google_workspace import EvidenceDiscoveryRequest, ObligacionInput
from app.services import evidence_discovery_service, integration_service
from sqlalchemy.ext.asyncio import AsyncSession


async def _passthrough_node(state: AgentState, **_kwargs: object) -> AgentState:
    return state


@pytest.fixture(autouse=True)
def _stub_downstream_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the LLM-backed dedup/filter/matcher/justify nodes as no-ops so
    these tests isolate the provider-gather fail-open contract, not the
    justification pipeline (which needs its own LLM mocking — out of scope
    for this file, see module docstring)."""
    for name in (
        "evidence_orchestrator_node",
        "evidence_dedup_node",
        "evidence_filter_node",
        "evidence_matcher_node",
        "evidence_justify_node",
    ):
        monkeypatch.setattr(evidence_discovery_service, name, _passthrough_node)


async def _connect_google(db: AsyncSession, usuario_id: uuid.UUID) -> None:
    await integration_service.store_credentials(
        db,
        usuario_id,
        IntegrationProvider.GOOGLE,
        access_token="access-token",
        refresh_token="refresh-token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


def _request() -> EvidenceDiscoveryRequest:
    return EvidenceDiscoveryRequest(obligaciones=[ObligacionInput(descripcion="Entregar informe mensual")])


class TestNoProviderConnectedGate:
    @pytest.mark.asyncio
    async def test_raises_external_service_error_with_exact_code(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]

        with pytest.raises(ExternalServiceError) as exc_info:
            await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert exc_info.value.code == NO_PROVIDER_CONNECTED

    @pytest.mark.asyncio
    async def test_maps_to_http_502(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.core.exceptions import domain_to_http

        user = test_user["user"]

        with pytest.raises(ExternalServiceError) as exc_info:
            await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert domain_to_http(exc_info.value).status_code == 502


class TestPerProviderFailOpen:
    @pytest.mark.asyncio
    async def test_email_search_failure_still_returns_200_with_zero_email_results(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.adapters.email.gmail_adapter import GmailAdapter

        user = test_user["user"]
        await _connect_google(db, user.id)
        monkeypatch.setattr(GmailAdapter, "search_messages", AsyncMock(side_effect=TimeoutError("gmail down")))
        monkeypatch.setattr(evidence_discovery_service, "drive_fetch_node", _passthrough_node)
        monkeypatch.setattr(evidence_discovery_service, "calendar_fetch_node", _passthrough_node)

        result = await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert result.fuentes["email"] == 0

    @pytest.mark.asyncio
    async def test_drive_down_does_not_block_email_evidence(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.adapters.email.gmail_adapter import GmailAdapter
        from app.adapters.email.port import EmailMessage

        user = test_user["user"]
        await _connect_google(db, user.id)

        fake_message = EmailMessage(
            id="m1",
            thread_id="t1",
            subject="Informe mensual",
            sender="supervisor@entidad.gov.co",
            recipients=["yo@example.com"],
            date=None,
            body_plain="Adjunto el informe mensual solicitado.",
            body_html=None,
            snippet="Adjunto el informe",
            attachments=[],
            labels=[],
            headers={},
        )
        monkeypatch.setattr(GmailAdapter, "search_messages", AsyncMock(return_value=[fake_message]))

        async def _failing_drive_fetch(state: AgentState, **_kwargs: object) -> AgentState:
            raise ExternalServiceError("Drive", "simulated outage")

        monkeypatch.setattr(evidence_discovery_service, "drive_fetch_node", _failing_drive_fetch)
        monkeypatch.setattr(evidence_discovery_service, "calendar_fetch_node", _passthrough_node)

        result = await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert result.fuentes["email"] >= 1
        assert result.fuentes["drive"] == 0

    @pytest.mark.asyncio
    async def test_drive_adapter_failure_at_the_real_seam_does_not_block_email_evidence(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every other Drive-down test in this class patches `drive_fetch_node`
        (the agent-graph node), never the real `DriveAdapter.search_files` seam
        — so the actual adapter -> node -> service propagation path was never
        exercised end-to-end. Review r1 finding P3c: patch the real adapter
        method instead, proving `drive_fetch_node`'s per-query try/except
        (app/agent/nodes/drive_fetch.py) genuinely swallows an
        `ExternalServiceError` raised by the adapter itself, not just one
        raised by a stand-in node."""
        from app.adapters.drive.drive_adapter import DriveAdapter
        from app.adapters.email.gmail_adapter import GmailAdapter
        from app.adapters.email.port import EmailMessage

        user = test_user["user"]
        await _connect_google(db, user.id)

        fake_message = EmailMessage(
            id="m1",
            thread_id="t1",
            subject="Informe mensual",
            sender="supervisor@entidad.gov.co",
            recipients=["yo@example.com"],
            date=None,
            body_plain="Adjunto el informe mensual solicitado.",
            body_html=None,
            snippet="Adjunto el informe",
            attachments=[],
            labels=[],
            headers={},
        )
        monkeypatch.setattr(GmailAdapter, "search_messages", AsyncMock(return_value=[fake_message]))
        monkeypatch.setattr(
            DriveAdapter, "search_files", AsyncMock(side_effect=ExternalServiceError("Drive", "simulated outage"))
        )
        monkeypatch.setattr(evidence_discovery_service, "calendar_fetch_node", _passthrough_node)

        result = await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert result.fuentes["email"] >= 1
        assert result.fuentes["drive"] == 0

    @pytest.mark.asyncio
    async def test_calendar_down_does_not_block_drive_evidence(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.adapters.email.gmail_adapter import GmailAdapter

        user = test_user["user"]
        await _connect_google(db, user.id)
        monkeypatch.setattr(GmailAdapter, "search_messages", AsyncMock(return_value=[]))

        async def _working_drive_fetch(state: AgentState, **_kwargs: object) -> AgentState:
            return {**state, "drive_evidencias": [{"title": "informe.pdf", "link": "https://drive.google.com/x"}]}

        async def _failing_calendar_fetch(state: AgentState, **_kwargs: object) -> AgentState:
            raise ExternalServiceError("Calendar", "simulated outage")

        monkeypatch.setattr(evidence_discovery_service, "drive_fetch_node", _working_drive_fetch)
        monkeypatch.setattr(evidence_discovery_service, "calendar_fetch_node", _failing_calendar_fetch)

        result = await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert result.fuentes["drive"] == 1
        assert result.fuentes["calendar"] == 0

    @pytest.mark.asyncio
    async def test_all_three_providers_down_still_returns_200_empty_not_502(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CONNECTED provider whose calls all fail is a different case from
        NO provider connected at all: the gate only checks connection, not
        liveness, so the request must still succeed with zero results, never
        502 NO_PROVIDER_CONNECTED."""
        from app.adapters.email.gmail_adapter import GmailAdapter

        user = test_user["user"]
        await _connect_google(db, user.id)
        monkeypatch.setattr(GmailAdapter, "search_messages", AsyncMock(side_effect=TimeoutError("gmail down")))

        async def _failing_drive_fetch(state: AgentState, **_kwargs: object) -> AgentState:
            raise ExternalServiceError("Drive", "simulated outage")

        async def _failing_calendar_fetch(state: AgentState, **_kwargs: object) -> AgentState:
            raise ExternalServiceError("Calendar", "simulated outage")

        monkeypatch.setattr(evidence_discovery_service, "drive_fetch_node", _failing_drive_fetch)
        monkeypatch.setattr(evidence_discovery_service, "calendar_fetch_node", _failing_calendar_fetch)

        result = await evidence_discovery_service.descubrir_evidencias(db, user.id, _request())

        assert result.fuentes == {"email": 0, "drive": 0, "calendar": 0, "local_file": 0}


class TestPerQueryFailOpenInGatherEmailEvidence:
    @pytest.mark.asyncio
    async def test_one_failing_query_does_not_drop_results_from_other_queries(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.adapters.email.gmail_adapter import GmailAdapter
        from app.adapters.email.port import EmailMessage

        user = test_user["user"]
        good_message = EmailMessage(
            id="ok1",
            thread_id="t1",
            subject="Informe entregado",
            sender="jefe@entidad.gov.co",
            recipients=["yo@example.com"],
            date=None,
            body_plain="cuerpo",
            body_html=None,
            snippet="cuerpo",
            attachments=[],
            labels=[],
            headers={},
        )

        calls = {"n": 0}

        async def _search_messages(self: object, usuario_id: object, query: str, max_results: int = 20) -> list:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("first query timed out")
            return [good_message]

        monkeypatch.setattr(GmailAdapter, "search_messages", _search_messages)

        emails, filtered = await evidence_discovery_service._gather_email_evidence(
            db,
            user.id,
            [{"id": "ob1", "descripcion": "Entregar informe mensual a la Secretaría"}],
            "2024-01-01",
            "2024-02-01",
            None,
            None,
            IntegrationProvider.GOOGLE,
        )

        assert calls["n"] >= 2, "expected at least 2 distinct queries to have been attempted"
        assert any(e["message_id"] == "ok1" for e in emails)
        assert isinstance(filtered, int)
