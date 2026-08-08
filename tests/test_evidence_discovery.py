"""Tests for the evidence_discovery_service — end-to-end orchestration (mocked Google + LLM)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.email.port import EmailMessage
from app.models.contrato import Contrato
from app.models.integracion import IntegrationProvider
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
        date=datetime(2024, 4, 10, tzinfo=UTC),
        body_plain=body,
        snippet=body[:80],
    )


def _connected_status(provider: IntegrationProvider = IntegrationProvider.GOOGLE):
    s = MagicMock()
    s.connected = True
    s.provider = provider
    return s


def _patch_only_google_connected(eds):
    """Patches the provider-agnostic gate so only Google is connected (Slice C2)."""
    return (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service, "list_integration_statuses", AsyncMock(return_value=[_connected_status()])
        ),
    )


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
        return_value=[
            _email("m1", "Informe mensual actividades", "Adjunto informe mensual de actividades del contrato de abril")
        ]
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
    justify_llm.complete = AsyncMock(
        return_value=MagicMock(content="Elaboré y entregué el informe mensual de actividades del contrato.")
    )

    only_google, only_google_statuses = _patch_only_google_connected(eds)
    with (
        only_google,
        only_google_statuses,
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

    only_google, only_google_statuses = _patch_only_google_connected(eds)
    with (
        only_google,
        only_google_statuses,
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
async def test_descubrir_evidencias_requires_a_connected_provider():
    """Zero connected providers (Google or Microsoft) raises NO_PROVIDER_CONNECTED."""
    from app.core.exceptions import ExternalServiceError
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"descripcion": "Asistir a reuniones"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=False)),
        pytest.raises(ExternalServiceError) as exc_info,
    ):
        await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)
    assert exc_info.value.code == "NO_PROVIDER_CONNECTED"


@pytest.mark.asyncio
async def test_descubrir_evidencias_succeeds_microsoft_only_connected():
    """A Microsoft-only connected user proceeds past the gate without error
    (evidence-discovery-gate spec: "User connected only to Microsoft")."""
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    ms_adapter = MagicMock()
    ms_adapter.search_messages = AsyncMock(return_value=[])
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No hay evidencia."))

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service,
            "list_integration_statuses",
            AsyncMock(return_value=[_connected_status(IntegrationProvider.MICROSOFT)]),
        ),
        patch.object(eds, "MicrosoftGraphAdapter", return_value=ms_adapter),
        patch("app.agent.nodes.drive_fetch.MicrosoftGraphAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.MicrosoftGraphAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)

    assert resp is not None  # reached the end of the pipeline — gate did not raise


@pytest.mark.asyncio
async def test_descubrir_evidencias_merges_both_providers_and_isolates_failure():
    """Both providers connected: evidence_raw has items from both. Microsoft failing
    after exhausting retries must not prevent Google's evidence from being returned
    (evidence-discovery-gate spec scenarios "both connected" / "Microsoft fails,
    Google succeeds")."""
    from app.core.exceptions import ExternalServiceError
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(
        return_value=[_email("g1", "Informe mensual actividades", "Adjunto informe mensual de abril")]
    )
    ms_adapter = MagicMock()
    ms_adapter.search_messages = AsyncMock(side_effect=ExternalServiceError("Microsoft Graph", "reintentos agotados"))
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(
        return_value=MagicMock(content="Elaboré y entregué el informe mensual de actividades del contrato.")
    )

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service,
            "list_integration_statuses",
            AsyncMock(
                return_value=[
                    _connected_status(IntegrationProvider.GOOGLE),
                    _connected_status(IntegrationProvider.MICROSOFT),
                ]
            ),
        ),
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch.object(eds, "MicrosoftGraphAdapter", return_value=ms_adapter),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.drive_fetch.MicrosoftGraphAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.calendar_fetch.MicrosoftGraphAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_filter.get_llm", return_value=AsyncMock()),
        patch("app.agent.nodes.evidence_matcher.get_llm", return_value=AsyncMock()),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)

    # Microsoft's email gather raised — Google's evidence must still be present.
    assert resp.total_evidencias >= 1
    assert resp.fuentes["email"] == 1


@pytest.mark.asyncio
async def test_descubrir_evidencias_dedupes_duplicate_evidence_across_providers():
    """The same underlying document surfaced by both providers is deduplicated by
    the existing (SHA-256 content hash) dedup logic (spec: "Duplicate evidence
    across providers is deduplicated")."""
    from app.adapters.drive.port import DriveFile
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    def _drive_file(fid: str) -> DriveFile:
        return DriveFile(
            id=fid,
            name="Informe mensual abril.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            created_at=datetime(2024, 4, 12, tzinfo=UTC),
            modified_at=datetime(2024, 4, 12, tzinfo=UTC),
            web_view_link=f"https://example.com/{fid}",
        )

    google_drive = MagicMock()
    google_drive.search_files = AsyncMock(return_value=[_drive_file("g-file")])
    ms_drive = MagicMock()
    ms_drive.search_files = AsyncMock(return_value=[_drive_file("ms-file")])
    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[])
    ms_mail = MagicMock()
    ms_mail.search_messages = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="Elaboré el informe mensual."))
    filter_llm = AsyncMock()
    filter_llm.complete = AsyncMock(return_value=MagicMock(content="[]"))

    # Spy in place of the real matcher: `fuentes` counts are computed from the
    # RAW per-source lists (drive_evidencias etc., unaffected by dedup — that's
    # pre-existing behavior, not part of this fix), so the only direct way to
    # observe that evidence_dedup_node collapsed the cross-provider duplicate
    # is to inspect what actually reaches the matcher (`state["evidence_raw"]`).
    captured: dict[str, int] = {}

    async def _spy_matcher(state):
        captured["evidence_raw_len"] = len(state.get("evidence_raw") or [])
        return {**state, "matched_evidence": {}, "current_phase": "evidence_matcher"}

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service,
            "list_integration_statuses",
            AsyncMock(
                return_value=[
                    _connected_status(IntegrationProvider.GOOGLE),
                    _connected_status(IntegrationProvider.MICROSOFT),
                ]
            ),
        ),
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch.object(eds, "MicrosoftGraphAdapter", return_value=ms_mail),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=google_drive),
        patch("app.agent.nodes.drive_fetch.MicrosoftGraphAdapter", return_value=ms_drive),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.calendar_fetch.MicrosoftGraphAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_filter.get_llm", return_value=filter_llm),
        patch.object(eds, "evidence_matcher_node", _spy_matcher),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req)

    # Same file name/content from both providers → the SHA-256 content-hash dedup
    # collapses them to a single evidence item before the matcher ever sees it.
    assert captured["evidence_raw_len"] == 1


@pytest.mark.asyncio
async def test_descubrir_evidencias_requires_obligaciones_or_contrato():
    from app.core.exceptions import ValidationError
    from app.services import evidence_discovery_service as eds

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

    Reuses the NO_PROVIDER_CONNECTED scenario (cheap to trigger, no LLM/Google
    mocking needed) so this also re-confirms the swap preserved error mapping.
    """
    spy = AsyncMock(side_effect=real_invoke_tool)

    with (
        patch("app.api.v1.integraciones.invoke_tool", spy),
        patch(
            "app.services.evidence_discovery_service.integration_service.has_any_connected_provider",
            AsyncMock(return_value=False),
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
    assert resp.json()["code"] == "NO_PROVIDER_CONNECTED"
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
        await eds._gather_email_evidence(
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

    req = EvidenceDiscoveryRequest(contrato_id=victim_contrato.id, fecha_inicio="2024-04-01", fecha_fin="2024-04-30")

    with pytest.raises(NotFoundError):
        await eds.descubrir_evidencias(db, attacker.id, req)


@pytest.mark.asyncio
async def test_descubrir_evidencias_propio_cuenta_id_no_lanza_not_found(db: AsyncSession) -> None:
    """A user's own cuenta_id must resolve past the ownership check (reaches the
    provider-connected gate, proving no NotFoundError was raised for legit ids).

    No `Integracion` row exists for this user in the real test DB, so the gate
    naturally raises NO_PROVIDER_CONNECTED — no mocking of the gate needed.
    """
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

    with pytest.raises(ExternalServiceError) as exc_info:
        await eds.descubrir_evidencias(db, user.id, req)
    assert exc_info.value.code == "NO_PROVIDER_CONNECTED"


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

    only_google, only_google_statuses = _patch_only_google_connected(eds)
    with (
        only_google,
        only_google_statuses,
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(db, user.id, req)

    assert "2024-02-01" in resp.resumen
    assert date.today().isoformat() in resp.resumen


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline unification: local_only mode (evidence-classification-pipeline)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_descubrir_evidencias_local_only_true_zero_google_calls(db: AsyncSession) -> None:
    """local_only=True must run the orchestrator→filter→matcher→justify pipeline
    to completion over uploaded local evidence WITHOUT touching Gmail/Drive/
    Calendar adapters, and WITHOUT ever evaluating the provider-connection gate
    (evidence-classification-pipeline: Zero external API calls in local-only mode,
    Local path never evaluates the provider gate)."""
    from app.models.actividad import Actividad
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.models.evidencia import Evidencia
    from app.services import evidence_discovery_service as eds

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1_000_000)
    db.add(cuenta)
    await db.flush()
    actividad = Actividad(cuenta_cobro_id=cuenta.id, descripcion="stub")
    db.add(actividad)
    await db.flush()
    db.add(
        Evidencia(
            actividad_id=actividad.id,
            storage_key="evidencias/x/informe.pdf",
            nombre_archivo="informe.pdf",
            tipo_archivo="application/pdf",
            tamano_bytes=10,
            texto_extraido="Entregué el informe mensual de actividades del contrato.",
        )
    )
    await db.commit()

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}],
        cuenta_id=cuenta.id,
    )

    gmail_ctor = MagicMock()
    drive_ctor = MagicMock()
    cal_ctor = MagicMock()
    gate_spy = AsyncMock()

    matcher_llm = AsyncMock()
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="[1]"))
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="Entregué el informe mensual."))

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", gate_spy),
        patch.object(eds, "GmailAdapter", gmail_ctor),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", drive_ctor),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", cal_ctor),
        patch("app.agent.nodes.evidence_matcher.get_llm", return_value=matcher_llm),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        resp = await eds.descubrir_evidencias(db, user.id, req, local_only=True)

    gmail_ctor.assert_not_called()
    drive_ctor.assert_not_called()
    cal_ctor.assert_not_called()
    gate_spy.assert_not_called()
    assert resp.fuentes["email"] == 0
    assert resp.fuentes["drive"] == 0
    assert resp.fuentes["calendar"] == 0
    assert resp.fuentes["local_file"] == 1
    assert resp.total_evidencias == 1


# ─────────────────────────────────────────────────────────────────────────────
# Discovery-result cache (radicar-ui-ux-improvements C.1 / design §5)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_descubrir_evidencias_second_call_same_window_hits_cache_no_google_calls(
    db: AsyncSession,
) -> None:
    """A second call for the same cuenta_id + fecha_inicio/fecha_fin within TTL must
    be served from the cache and MUST NOT re-invoke Gmail/Drive/Calendar adapters
    (design §5: consulted BEFORE the Gmail→Drive→Calendar chain runs)."""
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.services import discovery_cache
    from app.services import evidence_discovery_service as eds

    discovery_cache.clear()
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1_000_000)
    db.add(cuenta)
    await db.commit()

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
        cuenta_id=cuenta.id,
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[])
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No hay evidencia."))

    gmail_ctor = MagicMock(return_value=gmail)
    drive_ctor = MagicMock(return_value=drive_adapter)
    cal_ctor = MagicMock(return_value=cal_adapter)

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service, "list_integration_statuses", AsyncMock(return_value=[_connected_status()])
        ),
        patch.object(eds, "GmailAdapter", gmail_ctor),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", drive_ctor),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", cal_ctor),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        first = await eds.descubrir_evidencias(db, user.id, req)
        second = await eds.descubrir_evidencias(db, user.id, req)

    assert gmail_ctor.call_count == 1
    assert drive_ctor.call_count == 1
    assert cal_ctor.call_count == 1
    assert second.resumen == first.resumen


@pytest.mark.asyncio
async def test_descubrir_evidencias_different_window_forces_fresh_discovery(db: AsyncSession) -> None:
    """A different fecha_inicio/fecha_fin for the same cuenta_id is a cache miss —
    the Gmail/Drive/Calendar chain runs again."""
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.services import discovery_cache
    from app.services import evidence_discovery_service as eds

    discovery_cache.clear()
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1_000_000)
    db.add(cuenta)
    await db.commit()

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[])
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No hay evidencia."))

    gmail_ctor = MagicMock(return_value=gmail)

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service, "list_integration_statuses", AsyncMock(return_value=[_connected_status()])
        ),
        patch.object(eds, "GmailAdapter", gmail_ctor),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        req1 = EvidenceDiscoveryRequest(
            obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual"}],
            cuenta_id=cuenta.id,
            fecha_inicio="2024-04-01",
            fecha_fin="2024-04-30",
        )
        req2 = EvidenceDiscoveryRequest(
            obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual"}],
            cuenta_id=cuenta.id,
            fecha_inicio="2024-05-01",
            fecha_fin="2024-05-31",
        )
        await eds.descubrir_evidencias(db, user.id, req1)
        await eds.descubrir_evidencias(db, user.id, req2)

    assert gmail_ctor.call_count == 2


@pytest.mark.asyncio
async def test_descubrir_evidencias_refresh_true_bypasses_and_repopulates_cache(db: AsyncSession) -> None:
    """`refresh=True` bypasses a warm cache entry, re-runs the Gmail/Drive/Calendar
    chain, and repopulates the cache with the fresh result."""
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.services import discovery_cache
    from app.services import evidence_discovery_service as eds

    discovery_cache.clear()
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1_000_000)
    db.add(cuenta)
    await db.commit()

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"id": "ob1", "descripcion": "Entregar informe mensual"}],
        cuenta_id=cuenta.id,
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[])
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No hay evidencia."))

    gmail_ctor = MagicMock(return_value=gmail)

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(
            eds.integration_service, "list_integration_statuses", AsyncMock(return_value=[_connected_status()])
        ),
        patch.object(eds, "GmailAdapter", gmail_ctor),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        await eds.descubrir_evidencias(db, user.id, req)
        await eds.descubrir_evidencias(db, user.id, req)  # served from cache
        await eds.descubrir_evidencias(db, user.id, req, refresh=True)  # bypass + repopulate

    assert gmail_ctor.call_count == 2


@pytest.mark.asyncio
async def test_descubrir_evidencias_local_only_false_still_requires_provider_gate() -> None:
    """Explicit local_only=False (or omitted — the default) preserves the
    pre-existing connected-provider requirement unchanged: the gate decoupling
    applies ONLY to the local branch (evidence-classification-pipeline: provider
    gate preserved for Google/Microsoft-sourced evidence)."""
    from app.core.exceptions import ExternalServiceError
    from app.services import evidence_discovery_service as eds

    req = EvidenceDiscoveryRequest(
        obligaciones=[{"descripcion": "Asistir a reuniones"}],
        fecha_inicio="2024-04-01",
        fecha_fin="2024-04-30",
    )

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=False)),
        pytest.raises(ExternalServiceError),
    ):
        await eds.descubrir_evidencias(MagicMock(), uuid.uuid4(), req, local_only=False)
