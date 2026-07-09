"""Tests for the evidence_discovery_service — end-to-end orchestration (mocked Google + LLM)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.email.port import EmailMessage
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.google_workspace import EvidenceDiscoveryRequest
from app.tools.invoke import invoke_tool as real_invoke_tool
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


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
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="[1]", total_tokens=5))
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
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="[1]"))
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


@pytest.mark.asyncio
async def test_descubrir_evidencias_endpoint_routes_through_tool_registry(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    """POST /integraciones/evidencias/descubrir must dispatch through
    `invoke_tool("descubrir_evidencias", ...)` — the shared tool registry (same
    handler the /mcp server exposes) — rather than calling the service directly.

    Reuses the GOOGLE_NOT_CONNECTED scenario (cheap to trigger, no LLM/Google
    mocking needed) so this also re-confirms the swap preserved error mapping.
    """
    disconnected = MagicMock()
    disconnected.connected = False

    spy = AsyncMock(side_effect=real_invoke_tool)

    with (
        patch("app.api.v1.integraciones.invoke_tool", spy),
        patch(
            "app.services.evidence_discovery_service.gws.get_integration_status",
            AsyncMock(return_value=disconnected),
        ),
    ):
        resp = await client.post(
            "/api/v1/integraciones/evidencias/descubrir",
            headers=test_user["headers"],
            json={
                "obligaciones": [{"descripcion": "Asistir a reuniones"}],
                "fecha_inicio": "2024-04-01",
                "fecha_fin": "2024-04-30",
            },
        )

    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "GOOGLE_NOT_CONNECTED"
    spy.assert_awaited_once()
    assert spy.await_args is not None
    assert spy.await_args.args[0] == "descubrir_evidencias"


# ─────────────────────────────────────────────────────────────────────────────
# Max-effort fan-out: ALL obligaciones get queries, not just the first 3.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_gmail_evidence_queries_all_obligaciones_not_just_first_three():
    """5 obligaciones with distinct keywords → every one contributes at least one
    Gmail query. Previously only the first 3 obligaciones (`obligaciones[:3]`) were
    ever queried — this is the "maximum effort" fan-out fix."""
    from app.services import evidence_discovery_service as eds

    # Each description starts with a distinctive verb (>4 chars) — guaranteed to
    # survive both `_extract_keywords` (order-preserving) and the `keywords[:4]`
    # cap in `build_obligation_queries`, since it's always the first candidate.
    obligaciones = [
        {"id": f"ob{i}", "descripcion": desc}
        for i, desc in enumerate(
            [
                "Auditar sistemas informáticos gubernamentales locales",
                "Certificar procesos administrativos regionales anuales",
                "Diagnosticar infraestructuras comunitarias territoriales rurales",
                "Evaluar convenios interinstitucionales culturales nacionales",
                "Fiscalizar plataformas tecnológicas municipales digitales",
            ],
            start=1,
        )
    ]

    captured_queries: list[str] = []

    async def _fake_search(usuario_id, query, max_results):
        captured_queries.append(query)
        return []

    adapter = MagicMock()
    adapter.search_messages = AsyncMock(side_effect=_fake_search)

    with patch.object(eds, "GmailAdapter", return_value=adapter):
        await eds._gather_gmail_evidence(
            MagicMock(), uuid.uuid4(), obligaciones, "2024-04-01", "2024-04-30", None, None
        )

    # Each obligación's distinctive keyword-subject query must appear somewhere.
    keyword_terms = ["auditar", "certificar", "diagnosticar", "evaluar", "fiscalizar"]
    for term in keyword_terms:
        assert any(term in q for q in captured_queries), (
            f"expected a query mentioning '{term}' (obligación not queried) — captured: {captured_queries}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Date-range default from contrato when fecha_inicio/fecha_fin are omitted
# ─────────────────────────────────────────────────────────────────────────────


async def _make_user(db: AsyncSession) -> Usuario:
    user = Usuario(
        email="discovery-dates@test.com",
        nombre="Discovery Dates",
        cedula="111222333",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="CTR-DISC-001",
        objeto="Prestación de servicios",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 2, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    return contrato


# ─────────────────────────────────────────────────────────────────────────────
# IDOR: contrato/cuenta ownership must be verified before any evidence is gathered
# ─────────────────────────────────────────────────────────────────────────────


async def _make_victim_user(db: AsyncSession) -> Usuario:
    user = Usuario(
        email="victim-discovery@test.com",
        nombre="Victim",
        cedula="999888777",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_descubrir_evidencias_cuenta_id_ajena_lanza_not_found(db: AsyncSession) -> None:
    """Passing another user's cuenta_id must 404, not leak their contrato/obligaciones."""
    from app.core.exceptions import NotFoundError
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.services import evidence_discovery_service as eds

    attacker = await _make_user(db)
    victim = await _make_victim_user(db)
    victim_contrato = await _make_contrato(db, victim.id)
    victim_cuenta = CuentaCobro(
        contrato_id=victim_contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(victim_cuenta)
    await db.commit()

    req = EvidenceDiscoveryRequest(cuenta_id=victim_cuenta.id, fecha_inicio="2024-04-01", fecha_fin="2024-04-30")

    with pytest.raises(NotFoundError):
        await eds.descubrir_evidencias(db, attacker.id, req)


@pytest.mark.asyncio
async def test_descubrir_evidencias_contrato_id_ajeno_lanza_not_found(db: AsyncSession) -> None:
    """Passing another user's contrato_id must 404, not leak their obligaciones."""
    from app.core.exceptions import NotFoundError
    from app.services import evidence_discovery_service as eds

    attacker = await _make_user(db)
    victim = await _make_victim_user(db)
    victim_contrato = await _make_contrato(db, victim.id)
    await db.commit()

    req = EvidenceDiscoveryRequest(
        contrato_id=victim_contrato.id, fecha_inicio="2024-04-01", fecha_fin="2024-04-30"
    )

    with pytest.raises(NotFoundError):
        await eds.descubrir_evidencias(db, attacker.id, req)


@pytest.mark.asyncio
async def test_descubrir_evidencias_propio_cuenta_id_no_lanza_not_found(db: AsyncSession) -> None:
    """A user's own cuenta_id must resolve past the ownership check (reaches the
    Google-connected check, proving no NotFoundError was raised for legit ids)."""
    from app.core.exceptions import ExternalServiceError
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.services import evidence_discovery_service as eds

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cuenta)
    await db.commit()

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual"}],
        cuenta_id=cuenta.id,
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    disconnected = MagicMock()
    disconnected.connected = False
    with (
        patch.object(eds.gws, "get_integration_status", AsyncMock(return_value=disconnected)),
        pytest.raises(ExternalServiceError),
    ):
        await eds.descubrir_evidencias(db, user.id, req)


@pytest.mark.asyncio
async def test_descubrir_evidencias_default_fechas_desde_contrato(db: AsyncSession) -> None:
    """When fecha_inicio/fecha_fin are omitted but contrato_id is given, the service
    defaults fecha_inicio to the contrato's own fecha_inicio and fecha_fin to today —
    instead of silently requiring the frontend to always supply both."""
    from app.services import evidence_discovery_service as eds

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
        contrato_id=contrato.id,
    )

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[])
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No hay evidencia."))

    with (
        patch.object(eds.gws, "get_integration_status", AsyncMock(return_value=_connected_status())),
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(db, user.id, req)

    assert "2024-02-01" in resp.resumen
    assert date.today().isoformat() in resp.resumen
