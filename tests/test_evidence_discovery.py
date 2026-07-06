"""Tests for the evidence_discovery_service — end-to-end orchestration (mocked Google + LLM)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.email.port import EmailMessage
from app.schemas.google_workspace import EvidenceDiscoveryRequest


def _email(mid: str, subject: str, body: str) -> EmailMessage:
    return EmailMessage(
        id=mid,
        thread_id="t1",
        subject=subject,
        sender="supervisor@entidad.gov.co",
        recipients=["contratista@gmail.com"],
        date=datetime(2024, 4, 10, tzinfo=timezone.utc),
        body_plain=body,
        snippet=body[:80],
    )


def _connected_status():
    s = MagicMock()
    s.connected = True
    return s


@pytest.mark.asyncio
async def test_descubrir_evidencias_full_flow():
    """Gmail evidence flows through orchestrator → filter → matcher → justify and is returned with links."""
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
        supervisor_email="supervisor@entidad.gov.co",
    )

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(
        return_value=[_email("m1", "Informe mensual actividades", "Adjunto informe mensual de actividades del contrato de abril")]
    )

    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])

    filter_llm = AsyncMock()
    filter_llm.complete = AsyncMock(return_value=MagicMock(content='[{"idx": 0, "verdict": "TRABAJO"}]'))
    matcher_llm = AsyncMock()
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="RELEVANTE", total_tokens=5))
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="Elaboré y entregué el informe mensual de actividades del contrato."))

    with (
        patch.object(eds.gws, "get_integration_status", AsyncMock(return_value=_connected_status())),
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_filter.get_llm", return_value=filter_llm),
        patch("app.agent.nodes.evidence_matcher.get_llm", return_value=matcher_llm),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)

    assert resp.total_evidencias == 1
    assert resp.fuentes["email"] == 1
    assert len(resp.obligaciones) == 1
    ob = resp.obligaciones[0]
    assert ob.obligacion_id == "ob1"
    assert "informe" in ob.justificacion.lower()
    assert ob.evidencias[0].link.startswith("https://mail.google.com")
    assert ob.evidencias[0].source == "email"


@pytest.mark.asyncio
async def test_descubrir_evidencias_filters_promo_emails():
    """Correos de promo son descartados por heurística antes del matching."""
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    promo = _email("m2", "50% OFF en cursos online", "Gran oferta por tiempo limitado")
    promo_msg = MagicMock()
    promo_msg.id = "m2"
    promo_msg.subject = "50% OFF en cursos online"
    promo_msg.sender = "promo@deals.com"
    promo_msg.body_plain = "Gran oferta por tiempo limitado"
    promo_msg.snippet = "Gran oferta"
    promo_msg.date = promo.date
    promo_msg.labels = ["CATEGORY_PROMOTIONS"]

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[promo_msg])

    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])

    # El filtro heurístico descarta el email antes del LLM; matcher y justify no deben ser llamados
    filter_llm = AsyncMock()
    filter_llm.complete = AsyncMock(return_value=MagicMock(content="[]"))
    matcher_llm = AsyncMock()
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="RELEVANTE"))
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No se encontraron evidencias."))

    with (
        patch.object(eds.gws, "get_integration_status", AsyncMock(return_value=_connected_status())),
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_filter.get_llm", return_value=filter_llm),
        patch("app.agent.nodes.evidence_matcher.get_llm", return_value=matcher_llm),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)

    # El correo promocional no debe aparecer en ninguna evidencia
    all_evidence_links = [ev.link for ob in resp.obligaciones for ev in ob.evidencias]
    assert not any("m2" in link for link in all_evidence_links)
    # El resumen debe mencionar el descartado
    assert "descart" in resp.resumen


@pytest.mark.asyncio
async def test_descubrir_evidencias_requires_google_connected():
    from app.services import evidence_discovery_service as eds
    from app.core.exceptions import ExternalServiceError

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"descripcion": "Asistir a reuniones"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )
    disconnected = MagicMock()
    disconnected.connected = False

    with patch.object(eds.gws, "get_integration_status", AsyncMock(return_value=disconnected)):
        with pytest.raises(ExternalServiceError):
            await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)


@pytest.mark.asyncio
async def test_descubrir_evidencias_requires_obligaciones_or_contrato():
    from app.services import evidence_discovery_service as eds
    from app.core.exceptions import ValidationError

    req = EvidenceDiscoveryRequest(fecha_inicio="2024-04-01", fecha_fin="2024-04-30")

    with pytest.raises(ValidationError):
        await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)
