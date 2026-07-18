"""Tests for `stepper_state_service` (radicacion-stepper, work unit B4 —
highest attention: the cross-repo `StepperStateResponse` contract + the
contiguous-prefix resume algorithm + the double-charge invariant).

Batch B4a: unit tests for the four step-1..4 `complete` predicates, calling
the predicate functions directly (the aggregate `obtener_stepper_state`,
steps 5-7, the resume algorithm, the double-charge guard, and the full
contract-shape test all land in batch B4b — see apply-progress.md for the
split rationale).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import stepper_state_service
from sqlalchemy.ext.asyncio import AsyncSession

# No module-wide `pytestmark = pytest.mark.asyncio`: this file intentionally
# mixes async tests (DB-backed) with plain sync tests (pure predicate
# functions, e.g. `_step3_checklist`) — `asyncio_mode = "auto"` (pyproject.toml)
# already auto-detects async tests without needing the marker, and applying it
# module-wide would incorrectly mark the sync tests too.


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-STEPPER-STATE-001",
        objeto="Servicios profesionales para pruebas de stepper-state",
        valor_total=24_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual de prueba con texto suficientemente largo",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    # The `contrato` fixture already refreshed `contrato.obligaciones` (empty) via
    # its own `db.refresh(c)` BEFORE this row existed; within the same session's
    # identity map, later eager-load options on `contrato` won't overwrite an
    # already-populated (stale) collection. Force a real reload here so every
    # test sees the current DB state through `contrato.obligaciones`.
    await db.refresh(contrato)
    return ob


async def _make_cuenta(
    db: AsyncSession,
    contrato: Contrato,
    *,
    mes: int = 1,
    anio: int = 2024,
    numero_cuota: int | None = 1,
    posicion: PosicionCuota = PosicionCuota.PRIMERA,
    requisitos_modo: str | None = None,
    fecha_transaccion: date | None = None,
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        numero_cuota=numero_cuota,
        posicion=posicion,
        requisitos_modo=requisitos_modo,
        fecha_transaccion=fecha_transaccion,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _add_documento_contrato(
    db: AsyncSession,
    *,
    usuario_id: Any,
    contrato_id: Any,
    tipo: TipoDocumentoFuente,
    cuenta_cobro_id: Any | None = None,
) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=contrato_id,
        cuenta_cobro_id=cuenta_cobro_id,
        storage_key=f"documentos/{tipo.value}.pdf",
        nombre=f"{tipo.value}.pdf",
        tipo=tipo,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _add_actividad_con_evidencia(
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
    *,
    con_evidencia: bool = True,
) -> Actividad:
    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada",
        justificacion="Justificación",
        fecha_realizacion=date(2024, 1, 10),
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)
    if con_evidencia:
        ev = Evidencia(
            actividad_id=act.id,
            storage_key=f"evidencias/{act.id}/foto.jpg",
            nombre_archivo="foto.jpg",
            tipo_archivo="image/jpeg",
            tamano_bytes=12,
        )
        db.add(ev)
        await db.commit()
        # Same identity-map staleness pattern as above, now on `act.evidencias`
        # (already cached empty by the `db.refresh(act)` call before this row
        # existed) — force a real reload.
        await db.refresh(act)
    # `_make_cuenta`'s own `db.refresh(cc)` already loaded `cuenta.actividades`
    # (empty) via the same identity-map staleness pattern as `contrato.obligaciones`
    # above — force a real reload so the newly added actividad is visible.
    await db.refresh(cuenta)
    return act


async def _contrato_completo(db: AsyncSession, contrato: Contrato, usuario_id: Any) -> None:
    """Satisfy step 1's nivel-contrato mandatory docs via contract-level uploads:
    CONTRATO + RPC (recurring, every cuenta) plus CEDULA/RUT/ACTA_INICIO
    (obligatorio + solo_primera_cuenta — required because `_make_cuenta`
    defaults `posicion=PRIMERA`)."""
    for tipo in (
        TipoDocumentoFuente.CONTRATO,
        TipoDocumentoFuente.RPC,
        TipoDocumentoFuente.CEDULA,
        TipoDocumentoFuente.RUT,
        TipoDocumentoFuente.ACTA_INICIO,
    ):
        await _add_documento_contrato(db, usuario_id=usuario_id, contrato_id=contrato.id, tipo=tipo)


async def _cuenta_completo(db: AsyncSession, cuenta: CuentaCobro, usuario_id: Any) -> None:
    """Satisfy step 2's SS-planilla predicate (cuenta-scoped upload)."""
    await _add_documento_contrato(
        db,
        usuario_id=usuario_id,
        contrato_id=cuenta.contrato_id,
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        cuenta_cobro_id=cuenta.id,
    )


# ── B4a — one unit test per step-1..4 predicate (direct calls) ─────────────


async def test_step1_predicate_incomplete_without_mandatory_docs(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service._step1_contrato(db, cuenta, contrato)

    assert result.step == 1
    assert result.key == "contrato"
    assert result.complete is False
    assert result.blocking is True
    _ = user


async def test_step1_predicate_complete_with_obligaciones_and_mandatory_docs(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    await _contrato_completo(db, contrato, user.id)
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service._step1_contrato(db, cuenta, contrato)

    assert result.complete is True
    assert result.blocking is False


async def test_step2_predicate_incomplete_without_ss_planilla(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, numero_cuota=1)

    result = await stepper_state_service._step2_cuota(db, cuenta)

    assert result.step == 2
    assert result.key == "cuota"
    assert result.complete is False


async def test_step2_predicate_complete_with_numero_cuota_and_ss_planilla(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, numero_cuota=1)
    await _cuenta_completo(db, cuenta, user.id)

    result = await stepper_state_service._step2_cuota(db, cuenta)

    assert result.complete is True


def test_step3_predicate_incomplete_when_mode_not_chosen() -> None:
    cuenta = CuentaCobro(requisitos_modo=None)

    result = stepper_state_service._step3_checklist(cuenta)

    assert result.step == 3
    assert result.key == "checklist"
    assert result.complete is False
    assert result.code == "CHECKLIST_INCOMPLETE"


def test_step3_predicate_complete_when_mode_chosen() -> None:
    cuenta = CuentaCobro(requisitos_modo="estandar")

    result = stepper_state_service._step3_checklist(cuenta)

    assert result.complete is True
    assert result.code is None


async def test_step4_predicate_incomplete_without_actividad_per_obligacion(
    contrato: Contrato, obligacion: Obligacion
) -> None:
    result = stepper_state_service._step4_justificaciones(contrato, [])

    assert result.step == 4
    assert result.key == "justificaciones"
    assert result.complete is False


async def test_step4_predicate_complete_with_one_actividad_per_obligacion(
    db: AsyncSession, contrato: Contrato, obligacion: Obligacion
) -> None:
    cuenta = await _make_cuenta(db, contrato)
    act = await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=False)

    result = stepper_state_service._step4_justificaciones(contrato, [act])

    assert result.complete is True
