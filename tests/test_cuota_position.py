"""Tests for the cuota position model (billing-resilience-templates, slice #3).

Covers: model shape (task 3.2), the `CUOTA_POSITION_CONFLICT` write-time invariant
(tasks 3.9-3.10), and one-time-obligation semantics driven by the stored `posicion`
field instead of the old `_is_first_cuenta` read-time heuristic (task 3.11).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import CUOTA_POSITION_CONFLICT, ValidationError, domain_to_http
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.usuario import Usuario
from app.schemas.cuenta_cobro import CuentaCobroCreate, CuentaCobroUpdate
from app.services import checklist_service, cuenta_cobro_service
from sqlalchemy.ext.asyncio import AsyncSession

# Note: `asyncio_mode = "auto"` (pyproject.toml) — async tests need no marker. This
# module deliberately mixes sync (pure dataclass/HTTP-mapping) and async (DB-backed)
# tests, so no module-level `pytestmark` is set (it would misfire warnings on the
# sync ones) — mirrors `test_coherence_validator_service.py`.


async def _make_user(db: AsyncSession, *, creditos: int = 100, email: str = "cuota-pos@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Test User",
        cedula="999888777",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=creditos,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: Any) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="CTR-CUOTAPOS-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Ministerio de Pruebas",
        dependencia="Dirección de Innovación",
        supervisor_nombre="Juan Supervisor",
    )
    db.add(contrato)
    await db.flush()
    return contrato


# ── 3.2 — model shape ─────────────────────────────────────────────────────────


async def test_cuenta_cobro_accepts_cuota_position_fields(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        valor=1_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
        numero_cuota=1,
        posicion=PosicionCuota.PRIMERA,
        informe_final=False,
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)

    assert cuenta.numero_cuota == 1
    assert cuenta.posicion == PosicionCuota.PRIMERA
    assert cuenta.informe_final is False


def test_cuenta_cobro_numero_cuota_accepts_none() -> None:
    cuenta = CuentaCobro(
        contrato_id=None,  # type: ignore[arg-type]
        mes=1,
        anio=2024,
        valor=1_000_000,
        numero_cuota=None,
    )
    assert cuenta.numero_cuota is None


def test_cuota_position_conflict_code_maps_to_http_422() -> None:
    exc = ValidationError("posición duplicada", code=CUOTA_POSITION_CONFLICT)
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == 422
    assert exc.code == "CUOTA_POSITION_CONFLICT"


# ── 3.9/3.10 — write-time invariant ───────────────────────────────────────────


async def test_crear_cuenta_cobro_duplicate_informe_final_raises_conflict(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data1 = CuentaCobroCreate(
        contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("1000000.00"), informe_final=True
    )
    await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data1)
    await db.commit()

    data2 = CuentaCobroCreate(
        contrato_id=contrato.id, mes=2, anio=2024, valor=Decimal("1000000.00"), informe_final=True
    )
    with pytest.raises(ValidationError) as exc_info:
        await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data2)
    assert exc_info.value.code == CUOTA_POSITION_CONFLICT


async def test_actualizar_cuenta_cobro_duplicate_informe_final_raises_conflict(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data1 = CuentaCobroCreate(
        contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("1000000.00"), informe_final=True
    )
    await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data1)
    await db.commit()

    data2 = CuentaCobroCreate(contrato_id=contrato.id, mes=2, anio=2024, valor=Decimal("1000000.00"))
    cuenta2 = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data2)
    await db.commit()

    with pytest.raises(ValidationError) as exc_info:
        await cuenta_cobro_service.actualizar_cuenta_cobro(
            db, user.id, cuenta2.id, CuentaCobroUpdate(informe_final=True)
        )
    assert exc_info.value.code == CUOTA_POSITION_CONFLICT


async def test_verificar_conflicto_posicion_primera_duplicate_raises(db: AsyncSession) -> None:
    """Direct unit test of the `posicion=primera` branch of the guard.

    DEVIATION (documented, mirrors the pattern in slices #1/#2 tasks 1.22/2.18):
    under normal `crear_cuenta_cobro` usage, `posicion=primera` is always DERIVED
    (count of existing active cuotas == 0), so it can never naturally collide with
    an existing `primera` cuota through the public service call — the guard is a
    defensive backstop for a data race / direct-write edge case, not a
    user-triggerable path. Exercised directly against the private helper instead
    of through `crear_cuenta_cobro`.
    """
    from app.services.cuenta_cobro_service import _verificar_conflicto_posicion

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        valor=1_000_000,
        numero_cuota=1,
        posicion=PosicionCuota.PRIMERA,
    )
    db.add(cuenta)
    await db.commit()

    with pytest.raises(ValidationError) as exc_info:
        await _verificar_conflicto_posicion(db, contrato.id, posicion_primera=True)
    assert exc_info.value.code == CUOTA_POSITION_CONFLICT


async def test_crear_cuenta_cobro_no_conflict_when_informe_final_false(db: AsyncSession) -> None:
    """Regression guard: default (informe_final=False) creation never raises,
    even for a contrato's second cuota."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data1 = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("1000000.00"))
    await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data1)
    await db.commit()

    data2 = CuentaCobroCreate(contrato_id=contrato.id, mes=2, anio=2024, valor=Decimal("1000000.00"))
    resp2 = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data2)
    assert resp2.numero_cuota == 2
    assert resp2.posicion == PosicionCuota.RECURRENTE
    assert resp2.informe_final is False


# ── 3.11 — one-time obligation follows stored posicion ────────────────────────


async def _make_cuenta_con_posicion(
    db: AsyncSession, contrato: Contrato, mes: int, posicion: PosicionCuota, numero_cuota: int
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        numero_cuota=numero_cuota,
        posicion=posicion,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def test_one_time_obligation_required_when_posicion_primera(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    cuenta = await _make_cuenta_con_posicion(db, contrato, mes=1, posicion=PosicionCuota.PRIMERA, numero_cuota=1)

    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "DEPENDIENTES" in codigos


async def test_one_time_obligation_blank_for_recurrente(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    await _make_cuenta_con_posicion(db, contrato, mes=1, posicion=PosicionCuota.PRIMERA, numero_cuota=1)
    cuenta2 = await _make_cuenta_con_posicion(db, contrato, mes=2, posicion=PosicionCuota.RECURRENTE, numero_cuota=2)

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "DEPENDIENTES" not in codigos


async def test_one_time_obligation_follows_stored_posicion_not_chronology(db: AsyncSession) -> None:
    """Task 3.12: proves the checklist now reads the STORED `posicion` field rather
    than falling back to the old `_is_first_cuenta` chronological-order heuristic.

    Deliberately inverted setup: the RECURRENTE cuota has the EARLIER month
    (mes=1) and the PRIMERA cuota has a LATER month (mes=6) — the old
    "smallest (anio, mes)" heuristic would have wrongly picked the mes=1 cuota
    as "first" here. Only reading the stored `posicion` gets this right.
    """
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    cuenta_recurrente = await _make_cuenta_con_posicion(
        db, contrato, mes=1, posicion=PosicionCuota.RECURRENTE, numero_cuota=2
    )
    await _make_cuenta_con_posicion(db, contrato, mes=6, posicion=PosicionCuota.PRIMERA, numero_cuota=1)

    filas = await checklist_service.asegurar_checklist(db, cuenta_recurrente)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "DEPENDIENTES" not in codigos


async def test_one_time_obligation_blank_for_final(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    await _make_cuenta_con_posicion(db, contrato, mes=1, posicion=PosicionCuota.PRIMERA, numero_cuota=1)
    cuenta_final = CuentaCobro(
        contrato_id=contrato.id,
        mes=12,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        numero_cuota=2,
        posicion=PosicionCuota.FINAL,
        informe_final=True,
    )
    db.add(cuenta_final)
    await db.commit()
    await db.refresh(cuenta_final)

    filas = await checklist_service.asegurar_checklist(db, cuenta_final)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "DEPENDIENTES" not in codigos
