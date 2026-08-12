"""Tests for the P1 one-action fused evidencias flow (evidence_auto_service.auto_evidencias).

Collapses discovery → persistencia → justificación (previously 2-3 manual clicks across
`evidencias-tab.tsx` and `step-6-justificacion.tsx`) into one backend call. Covers:
- Gate-met fused run (discovery + persist happen as one operation).
- Repeat-trigger idempotency: no duplicate `Evidencia` rows, and no destructive
  overwrite of an already-justified `Actividad` (unlike the manual flow, which
  intentionally replaces text on every "Generar" click — see test_evidence_persist.py).
- Empty discovery is a clean no-op (`omitido=True`), same as "no provider connected".

Mocks only the discovery boundary (`evidence_discovery_service.descubrir_evidencias`,
already covered end-to-end by test_evidence_discovery.py) — persistence runs for real
against the test DB, since that's the idempotency logic under test.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import NO_PROVIDER_CONNECTED, ExternalServiceError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.google_workspace import EvidenceDiscoveryResponse, EvidenceLink, ObligacionJustificada
from app.services import evidence_auto_service
from app.services.informe_constants import SENTINEL_SIN_EVIDENCIAS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures (mirrors tests/test_evidence_persist.py) ──────────────────────────


async def _make_user(db: AsyncSession, *, email: str = "auto@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Auto User",
        cedula="112233445",
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
        numero_contrato="CTR-AUTO-001",
        objeto="Prestación de servicios",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    return contrato


async def _make_obligacion(db: AsyncSession, contrato_id: uuid.UUID, orden: int = 1) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=f"Obligación contractual {orden}",
        tipo=TipoObligacion.ESPECIFICA,
        orden=orden,
    )
    db.add(ob)
    await db.flush()
    return ob


async def _make_cuenta(db: AsyncSession, contrato_id: uuid.UUID) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato_id,
        mes=3,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cuenta)
    await db.flush()
    return cuenta


@pytest.fixture
async def scenario(db: AsyncSession) -> dict[str, Any]:
    # NOTE: flush (not commit+refresh) the CuentaCobro — its `actividades` relationship
    # uses lazy="selectin" and refreshing would cache an empty collection on this
    # identity-mapped instance, hiding Actividad rows created later in the same session
    # (same gotcha documented in tests/test_evidence_persist.py).
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()
    return {"user": user, "contrato": contrato, "obligacion": obligacion, "cuenta": cuenta}


def _discovery_response(
    obligacion: Obligacion,
    *,
    justificacion: str = "Justificación generada por el agente.",
    link: str = "https://mail.google.com/mail/u/0/#all/abc123",
    origen: str = "llm",
) -> EvidenceDiscoveryResponse:
    return EvidenceDiscoveryResponse(
        obligaciones=[
            ObligacionJustificada(
                obligacion_id=str(obligacion.id),
                descripcion=obligacion.descripcion,
                actividad="Actividad redactada por el agente.",
                justificacion=justificacion,
                origen=origen,
                evidencias=[EvidenceLink(source="email", titulo="Informe mensual", link=link, fecha="2024-03-10")],
            )
        ],
        resumen="Exploré Gmail para 1 obligación.",
        total_evidencias=1,
        fuentes={"email": 1},
    )


def _discovery_response_multi(
    obligaciones: list[Obligacion],
    *,
    justificacion: str = "Justificación generada por el agente.",
    origen: str = "llm",
) -> EvidenceDiscoveryResponse:
    return EvidenceDiscoveryResponse(
        obligaciones=[
            ObligacionJustificada(
                obligacion_id=str(ob.id),
                descripcion=ob.descripcion,
                actividad="Actividad redactada por el agente.",
                justificacion=justificacion,
                origen=origen,
                evidencias=[
                    EvidenceLink(
                        source="email",
                        titulo="Informe mensual",
                        link=f"https://mail.google.com/mail/u/0/#all/{ob.id}",
                        fecha="2024-03-10",
                    )
                ],
            )
            for ob in obligaciones
        ],
        resumen=f"Exploré Gmail para {len(obligaciones)} obligaciones.",
        total_evidencias=len(obligaciones),
        fuentes={"email": len(obligaciones)},
    )


def _empty_discovery_response() -> EvidenceDiscoveryResponse:
    return EvidenceDiscoveryResponse(obligaciones=[], resumen="Nada encontrado.", total_evidencias=0, fuentes={})


def _mock_discover(response: EvidenceDiscoveryResponse | Exception):
    mocked = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
    return patch.object(evidence_auto_service.evidence_discovery_service, "descubrir_evidencias", mocked)


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_auto_evidencias_gate_met_fused_run(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """Discovery + persistencia run as one operation and both get reflected in the summary."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]

    with _mock_discover(_discovery_response(obligacion)):
        result = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)

    assert result.omitido is False
    assert result.descubiertas == 1
    assert result.persistidas == 1
    assert result.justificadas == 1

    rows = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion.id))
    actividad = rows.scalar_one()
    assert actividad.justificacion == "Justificación generada por el agente."

    ev_rows = await db.execute(select(Evidencia).where(Evidencia.actividad_id == actividad.id))
    assert len(ev_rows.scalars().all()) == 1


async def test_auto_evidencias_repeat_trigger_no_duplica_ni_sobrescribe(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """Repeating the fused action: no duplicate Evidencia rows, and the already-justified
    Actividad is NOT destructively overwritten by a second (different) discovery result —
    the opposite of the manual flow's "replace on regenerate" behavior."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]

    with _mock_discover(_discovery_response(obligacion, justificacion="Primera justificación.")):
        primera = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)
    assert primera.justificadas == 1

    # Same evidence link (dedup by URL) + a DIFFERENT generated text (simulates LLM
    # non-determinism on a second discovery run for the same cuenta).
    with _mock_discover(_discovery_response(obligacion, justificacion="Segunda justificación distinta.")):
        segunda = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)

    assert segunda.omitido is False
    assert segunda.descubiertas == 1
    assert segunda.persistidas == 0  # same link → deduped, no new Evidencia row
    assert segunda.justificadas == 0  # already-justified cuenta → text NOT rewritten

    rows = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion.id))
    actividad = rows.scalar_one()
    assert actividad.justificacion == "Primera justificación."  # unchanged, not overwritten

    ev_rows = await db.execute(select(Evidencia).where(Evidencia.actividad_id == actividad.id))
    assert len(ev_rows.scalars().all()) == 1  # no duplicate row


async def test_auto_evidencias_cuenta_mixta_solo_bloquea_la_obligacion_ya_justificada(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """RELIABILITY-002: the "already justified, don't overwrite" guard must be
    scoped PER-OBLIGACIÓN, not per-cuenta. A cuenta with a mix of an already-
    justified obligación and a never-justified one must still get real
    justificación text written for the untouched one on this auto-fire, while
    the already-justified one's existing text is preserved unchanged."""
    user, contrato, cuenta = scenario["user"], scenario["contrato"], scenario["cuenta"]
    obligacion_justificada = scenario["obligacion"]
    obligacion_pendiente = await _make_obligacion(db, contrato.id, orden=2)
    await db.commit()

    ya = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion_justificada.id,
        descripcion="Actividad ya justificada de una corrida previa.",
        justificacion="Justificación previa que no debe cambiar.",
        justificacion_origen="llm",
    )
    db.add(ya)
    await db.commit()

    response = _discovery_response_multi(
        [obligacion_justificada, obligacion_pendiente],
        justificacion="Nueva justificación de esta corrida.",
    )
    with _mock_discover(response):
        result = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)

    assert result.omitido is False
    assert result.justificadas == 1  # only the pending obligación gets new text

    rows = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion_justificada.id))
    act_ya = rows.scalar_one()
    assert act_ya.justificacion == "Justificación previa que no debe cambiar."  # untouched

    rows = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion_pendiente.id))
    act_pendiente = rows.scalar_one()
    assert act_pendiente.justificacion == "Nueva justificación de esta corrida."  # written


async def test_auto_evidencias_discovery_vacia_es_no_op_limpio(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """Empty discovery completes without error, persists nothing, and does not loop."""
    user, cuenta = scenario["user"], scenario["cuenta"]

    with _mock_discover(_empty_discovery_response()):
        result = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)

    assert result.omitido is True
    assert result.descubiertas == 0
    assert result.persistidas == 0
    assert result.justificadas == 0

    rows = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    assert rows.scalars().all() == []


async def test_auto_evidencias_sin_proveedor_conectado_es_no_op_limpio(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """No Google/Microsoft connected: auto-fire must not raise — clean no-op, same as empty."""
    user, cuenta = scenario["user"], scenario["cuenta"]
    error = ExternalServiceError("Integraciones", "Ninguna cuenta está conectada.", code=NO_PROVIDER_CONNECTED)

    with _mock_discover(error):
        result = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)

    assert result.omitido is True
    assert result.descubiertas == 0
    assert result.persistidas == 0
    assert result.justificadas == 0


async def test_auto_evidencias_endpoint_happy_path(client, db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()
    await db.refresh(obligacion)
    await db.refresh(cuenta)

    with _mock_discover(_discovery_response(obligacion)):
        resp = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta.id}/evidencias/auto",
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["omitido"] is False
    assert data["descubiertas"] == 1
    assert data["persistidas"] == 1
    assert data["justificadas"] == 1


async def test_auto_evidencias_ignora_sentinela_como_ya_justificada(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """A sentinel-only justificación (previous 'sin evidencias' run) must NOT count as
    'already justified' — the next fused run should still be allowed to write real text."""
    obligacion, cuenta = scenario["obligacion"], scenario["cuenta"]
    user = scenario["user"]

    sentinela = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad sin evidencia previa.",
        justificacion=SENTINEL_SIN_EVIDENCIAS,
    )
    db.add(sentinela)
    await db.commit()

    with _mock_discover(_discovery_response(obligacion, justificacion="Justificación real generada ahora.")):
        result = await evidence_auto_service.auto_evidencias(db, user.id, cuenta.id)

    assert result.justificadas == 1
    await db.refresh(sentinela)
    assert sentinela.justificacion == "Justificación real generada ahora."
