"""CuentaCobro service unit tests."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import (
    AlreadyExistsError,
    ForbiddenError,
    InsufficientCreditsError,
    NotFoundError,
    ValidationError,
)
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.cuenta_cobro import ActividadCreate, CuentaCobroCreate, CuentaCobroUpdate
from app.services import cuenta_cobro_service
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, *, creditos: int = 100, email: str = "u@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Test User",
        cedula="123456789",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=creditos,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="001-2024",
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


async def _make_cuenta(
    db: AsyncSession,
    contrato_id: uuid.UUID,
    *,
    mes: int = 3,
    anio: int = 2024,
    estado: EstadoCuentaCobro = EstadoCuentaCobro.BORRADOR,
) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato_id,
        mes=mes,
        anio=anio,
        valor=3_000_000,
        estado=estado,
    )
    db.add(cuenta)
    await db.flush()
    return cuenta


# ---------------------------------------------------------------------------
# crear_cuenta_cobro
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_ok(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    resp = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)

    assert resp.contrato_id == contrato.id
    assert resp.mes == 1
    assert resp.anio == 2024
    assert resp.estado == EstadoCuentaCobro.BORRADOR

    # Credits should be deducted
    await db.refresh(user)
    assert user.creditos_disponibles == 90


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_first_cuota_deriva_primera_y_numero_1(db: AsyncSession) -> None:
    """Task 3.7/3.8 (billing-resilience-templates slice #3): the first cuota of a
    contrato derives and persists `posicion=primera`, `numero_cuota=1`."""
    from app.models.cuenta_cobro import PosicionCuota

    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    resp = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)

    assert resp.numero_cuota == 1
    assert resp.posicion == PosicionCuota.PRIMERA
    assert resp.informe_final is False


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_second_cuota_deriva_recurrente_y_numero_2(db: AsyncSession) -> None:
    from app.models.cuenta_cobro import PosicionCuota

    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data1 = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data1)
    await db.commit()

    data2 = CuentaCobroCreate(contrato_id=contrato.id, mes=2, anio=2024, valor=Decimal("3000000.00"))
    resp2 = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data2)

    assert resp2.numero_cuota == 2
    assert resp2.posicion == PosicionCuota.RECURRENTE


@pytest.mark.asyncio
async def test_obtener_cuenta_cobro_incluye_campos_cuota_position(db: AsyncSession) -> None:
    """Task 3.13: `obtener_cuenta_cobro` output includes the new cuota-position fields."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    created = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)
    await db.commit()

    fetched = await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, created.id)
    assert fetched.numero_cuota == 1
    assert fetched.posicion == created.posicion
    assert fetched.informe_final is False


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_insufficient_credits(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=5)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    with pytest.raises(InsufficientCreditsError):
        await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_contrato_not_found(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=100)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=uuid.uuid4(), mes=1, anio=2024, valor=Decimal("3000000.00"))
    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_contrato_otro_usuario(db: AsyncSession) -> None:
    owner = await _make_user(db, email="owner@test.com", creditos=100)
    other = await _make_user(db, email="other@test.com", creditos=100)
    contrato = await _make_contrato(db, owner.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.crear_cuenta_cobro(db, other.id, data)


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_duplicada(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=3, anio=2024, valor=Decimal("3000000.00"))
    await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)
    await db.commit()

    # Reload user to get updated credits (90 left)
    await db.refresh(user)

    with pytest.raises(AlreadyExistsError):
        await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)


# ---------------------------------------------------------------------------
# crear_cuenta_cobro — valor defaults from contrato.valor_mensual (B.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_sin_valor_usa_valor_mensual_del_contrato(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)  # valor_mensual=3_000_000
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024)
    resp = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)

    assert resp.valor == Decimal("3000000.00")


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_con_valor_explicito_sobreescribe_valor_mensual(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)  # valor_mensual=3_000_000
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("5000000.00"))
    resp = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)

    assert resp.valor == Decimal("5000000.00")


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_sin_valor_ni_valor_mensual_falla(db: AsyncSession) -> None:
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    contrato.valor_mensual = 0  # simulate a contrato with no monthly value configured
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024)
    with pytest.raises(ValidationError):
        await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)


# ---------------------------------------------------------------------------
# fecha_transaccion (radicacion-stepper, work unit B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_persiste_fecha_transaccion(db: AsyncSession) -> None:
    """CuentaCobroCreate.fecha_transaccion is stored on the created CuentaCobro row."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        valor=Decimal("3000000.00"),
        fecha_transaccion=date(2024, 1, 15),
    )
    resp = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)

    assert resp.fecha_transaccion == date(2024, 1, 15)

    persisted = await db.get(CuentaCobro, resp.id)
    assert persisted is not None
    assert persisted.fecha_transaccion == date(2024, 1, 15)


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_sin_fecha_transaccion_queda_null(db: AsyncSession) -> None:
    """Omitting fecha_transaccion leaves the column NULL (optional field)."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    await db.commit()

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=1, anio=2024, valor=Decimal("3000000.00"))
    resp = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)

    assert resp.fecha_transaccion is None


@pytest.mark.asyncio
async def test_cuenta_cobro_legacy_row_carga_fecha_transaccion_null(db: AsyncSession) -> None:
    """Legacy rows created directly on the model (pre-existing, without the new
    field set) load with fecha_transaccion=None — the column is additive and
    non-breaking."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    fetched = await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta.id)
    assert fetched.fecha_transaccion is None


@pytest.mark.asyncio
async def test_actualizar_cuenta_cobro_persiste_fecha_transaccion(db: AsyncSession) -> None:
    """PATCH with fecha_transaccion on a BORRADOR cuota persists the new date
    (radicacion-stepper step-2 resume-mode edit, WARNING 6 follow-up)."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await cuenta_cobro_service.actualizar_cuenta_cobro(
        db, user.id, cuenta.id, CuentaCobroUpdate(fecha_transaccion=date(2024, 3, 20))
    )

    assert resp.fecha_transaccion == date(2024, 3, 20)
    persisted = await db.get(CuentaCobro, cuenta.id)
    assert persisted is not None
    assert persisted.fecha_transaccion == date(2024, 3, 20)


@pytest.mark.asyncio
async def test_actualizar_cuenta_cobro_limpia_fecha_transaccion_con_null(db: AsyncSession) -> None:
    """PATCH sending fecha_transaccion=null on a cuota that already has a date
    clears it — null is an explicit, meaningful value, not 'omitted'."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    cuenta.fecha_transaccion = date(2024, 3, 20)
    await db.commit()

    resp = await cuenta_cobro_service.actualizar_cuenta_cobro(
        db, user.id, cuenta.id, CuentaCobroUpdate(fecha_transaccion=None)
    )

    assert resp.fecha_transaccion is None
    persisted = await db.get(CuentaCobro, cuenta.id)
    assert persisted is not None
    assert persisted.fecha_transaccion is None


@pytest.mark.asyncio
async def test_actualizar_cuenta_cobro_omitir_fecha_transaccion_no_la_toca(db: AsyncSession) -> None:
    """PATCH that does NOT include fecha_transaccion leaves the existing date
    untouched (distinguishes 'omitted' from 'explicit null' via model_fields_set)."""
    user = await _make_user(db, creditos=100)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    cuenta.fecha_transaccion = date(2024, 3, 20)
    await db.commit()

    resp = await cuenta_cobro_service.actualizar_cuenta_cobro(
        db, user.id, cuenta.id, CuentaCobroUpdate(valor=Decimal("4000000.00"))
    )

    assert resp.fecha_transaccion == date(2024, 3, 20)
    persisted = await db.get(CuentaCobro, cuenta.id)
    assert persisted is not None
    assert persisted.fecha_transaccion == date(2024, 3, 20)


# ---------------------------------------------------------------------------
# listar / obtener
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_vacia(db: AsyncSession) -> None:
    user = await _make_user(db)
    await db.commit()
    result = await cuenta_cobro_service.listar_cuentas_cobro(db, user.id)
    assert result == []


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_devuelve_solo_las_del_usuario(db: AsyncSession) -> None:
    owner = await _make_user(db, email="owner@test.com")
    other = await _make_user(db, email="other@test.com")
    contrato_owner = await _make_contrato(db, owner.id)
    contrato_other = await _make_contrato(db, other.id)
    await _make_cuenta(db, contrato_owner.id, mes=1)
    await _make_cuenta(db, contrato_owner.id, mes=2)
    await _make_cuenta(db, contrato_other.id, mes=1)
    await db.commit()

    result = await cuenta_cobro_service.listar_cuentas_cobro(db, owner.id)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_obtener_cuenta_cobro_ok(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta.id)
    assert resp.id == cuenta.id


@pytest.mark.asyncio
async def test_obtener_cuenta_cobro_not_found(db: AsyncSession) -> None:
    user = await _make_user(db)
    await db.commit()
    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_obtener_cuenta_cobro_otro_usuario(db: AsyncSession) -> None:
    owner = await _make_user(db, email="owner@test.com")
    other = await _make_user(db, email="other@test.com")
    contrato = await _make_contrato(db, owner.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    with pytest.raises(ForbiddenError):
        await cuenta_cobro_service.obtener_cuenta_cobro(db, other.id, cuenta.id)


# ---------------------------------------------------------------------------
# agregar_actividad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agregar_actividad_ok(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.BORRADOR)
    await db.commit()

    data = ActividadCreate(descripcion="Redacté el informe mensual de avance del proyecto")
    resp = await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta.id, data)
    assert resp.descripcion == data.descripcion


@pytest.mark.asyncio
async def test_agregar_actividad_en_rechazada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.RECHAZADA)
    await db.commit()

    data = ActividadCreate(descripcion="Corrección de actividad según feedback del supervisor")
    resp = await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta.id, data)
    assert resp.id is not None


@pytest.mark.asyncio
async def test_agregar_actividad_estado_invalido(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    await db.commit()

    data = ActividadCreate(descripcion="Esta actividad no debería poder agregarse")
    with pytest.raises(ValidationError):
        await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta.id, data)


@pytest.mark.asyncio
async def test_agregar_actividad_con_obligacion(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    obligacion = Obligacion(
        contrato_id=contrato.id,
        descripcion="Rendir informes mensuales de actividades",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    db.add(obligacion)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    data = ActividadCreate(
        descripcion="Entrega del informe mensual de actividades al supervisor",
        obligacion_id=obligacion.id,
    )
    resp = await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta.id, data)
    assert resp.obligacion_id == obligacion.id


@pytest.mark.asyncio
async def test_agregar_actividad_obligacion_de_otro_contrato(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato1 = await _make_contrato(db, user.id)
    contrato2 = await _make_contrato(db, user.id)
    obligacion_ajena = Obligacion(
        contrato_id=contrato2.id,
        descripcion="Obligación del contrato 2",
        tipo=TipoObligacion.GENERAL,
        orden=1,
    )
    db.add(obligacion_ajena)
    cuenta = await _make_cuenta(db, contrato1.id)
    await db.commit()

    data = ActividadCreate(
        descripcion="Actividad con obligación de otro contrato",
        obligacion_id=obligacion_ajena.id,
    )
    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta.id, data)


# ---------------------------------------------------------------------------
# cambiar_estado — state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cambiar_estado_borrador_a_enviada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.BORRADOR)
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.ENVIADA)
    assert resp.estado == EstadoCuentaCobro.ENVIADA
    assert resp.fecha_envio is not None


@pytest.mark.asyncio
async def test_cambiar_estado_enviada_a_aprobada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.APROBADA)
    assert resp.estado == EstadoCuentaCobro.APROBADA


@pytest.mark.asyncio
async def test_cambiar_estado_enviada_a_rechazada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.RECHAZADA)
    assert resp.estado == EstadoCuentaCobro.RECHAZADA


@pytest.mark.asyncio
async def test_cambiar_estado_rechazada_a_borrador(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.RECHAZADA)
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.BORRADOR)
    assert resp.estado == EstadoCuentaCobro.BORRADOR


@pytest.mark.asyncio
async def test_cambiar_estado_aprobada_a_pagada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.APROBADA)
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.PAGADA)
    assert resp.estado == EstadoCuentaCobro.PAGADA


@pytest.mark.asyncio
async def test_cambiar_estado_transicion_invalida(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.BORRADOR)
    await db.commit()

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.APROBADA)


@pytest.mark.asyncio
async def test_cambiar_estado_pagada_es_terminal(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.PAGADA)
    await db.commit()

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.BORRADOR)


@pytest.mark.asyncio
async def test_cambiar_estado_enviada_a_borrador_reabre_y_limpia_fecha_envio(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    cuenta.fecha_envio = datetime.now(UTC)
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.BORRADOR)
    assert resp.estado == EstadoCuentaCobro.BORRADOR
    assert resp.fecha_envio is None


@pytest.mark.asyncio
async def test_cambiar_estado_enviada_a_borrador_limpia_pdf_storage_key(db: AsyncSession) -> None:
    """REL-002: reopening must null the stale-PDF reference too, not just fecha_envio —
    otherwise obtener_url_pdf / email / Drive senders can still serve the pre-edit PDF."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    cuenta.pdf_storage_key = "pdfs/fake/stale.pdf"
    await db.commit()

    resp = await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.BORRADOR)
    assert resp.pdf_storage_key is None


@pytest.mark.asyncio
async def test_obtener_url_pdf_tras_reabrir_falla_hasta_regenerar(db: AsyncSession) -> None:
    """REL-002 companion: after reopen, obtener_url_pdf must fail cleanly (same
    convention as never having generated a PDF) instead of serving the stale one."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    cuenta.pdf_storage_key = "pdfs/fake/stale.pdf"
    await db.commit()

    await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.BORRADOR)
    await db.commit()

    mock_storage = AsyncMock()
    with pytest.raises(ValidationError):
        await cuenta_cobro_service.obtener_url_pdf(db, user.id, cuenta.id, mock_storage)


@pytest.mark.asyncio
async def test_cambiar_estado_aprobada_a_borrador_transicion_invalida(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.APROBADA)
    await db.commit()

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta.id, EstadoCuentaCobro.BORRADOR)


# ---------------------------------------------------------------------------
# eliminar_cuenta_cobro
# ---------------------------------------------------------------------------


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=[])
    return storage


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_borrador(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.BORRADOR)
    await db.commit()

    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id, _mock_storage())
    await db.commit()

    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta.id)


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_enviada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    await db.commit()

    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id, _mock_storage())
    await db.commit()

    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta.id)


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_rechazada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.RECHAZADA)
    await db.commit()

    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id, _mock_storage())
    await db.commit()

    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta.id)


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_aprobada_falla(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.APROBADA)
    await db.commit()

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id, _mock_storage())


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_pagada_falla(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.PAGADA)
    await db.commit()

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id, _mock_storage())


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_otro_usuario_falla(db: AsyncSession) -> None:
    """Pinning existing ownership behavior: cross-user delete is NotFoundError, not a
    silent no-op or a different error type."""
    owner = await _make_user(db)
    otro = await _make_user(db, email="otro@test.com")
    contrato = await _make_contrato(db, owner.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.BORRADOR)
    await db.commit()

    with pytest.raises(ForbiddenError):
        await cuenta_cobro_service.eliminar_cuenta_cobro(db, otro.id, cuenta.id, _mock_storage())


# ---------------------------------------------------------------------------
# generar_pdf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generar_pdf_ok(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    # Mock storage and WeasyPrint
    mock_storage = AsyncMock()
    mock_storage.upload = AsyncMock(return_value=f"pdfs/{user.id}/{cuenta.id}.pdf")
    mock_storage.presigned_url = AsyncMock(return_value="https://storage.example.com/presigned")

    import unittest.mock as mock

    with mock.patch("app.services.cuenta_cobro_service.generate_pdf_from_html", return_value=b"%PDF-fake"):
        resp = await cuenta_cobro_service.generar_pdf(db, user.id, cuenta.id, mock_storage)

    assert resp.pdf_url == "https://storage.example.com/presigned"
    assert "pdfs/" in resp.pdf_storage_key
    mock_storage.upload.assert_called_once()

    # Key should be persisted on the model
    await db.refresh(cuenta)
    assert cuenta.pdf_storage_key is not None


@pytest.mark.asyncio
async def test_obtener_url_pdf_sin_pdf_generado(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    mock_storage = AsyncMock()
    with pytest.raises(ValidationError):
        await cuenta_cobro_service.obtener_url_pdf(db, user.id, cuenta.id, mock_storage)


# ---------------------------------------------------------------------------
# _contexto_evidencias_por_obligacion (justification-context capability)
# Contract tests for the per-obligación context builder that replaces the
# flat top-10/1500-char query in generar_actividades_agente.
# ---------------------------------------------------------------------------


async def _make_obligacion(db: AsyncSession, contrato_id: uuid.UUID, descripcion: str, orden: int = 1) -> Obligacion:
    ob = Obligacion(contrato_id=contrato_id, descripcion=descripcion, tipo=TipoObligacion.ESPECIFICA, orden=orden)
    db.add(ob)
    await db.flush()
    return ob


async def _make_actividad_con_evidencia(db: AsyncSession, cuenta_id: uuid.UUID, nombre: str, texto: str):
    from app.models.actividad import Actividad
    from app.models.evidencia import Evidencia

    act = Actividad(cuenta_cobro_id=cuenta_id, descripcion="stub")
    db.add(act)
    await db.flush()
    ev = Evidencia(
        actividad_id=act.id,
        storage_key=f"evidencias/x/{nombre}",
        nombre_archivo=nombre,
        tipo_archivo="text/plain",
        tamano_bytes=len(texto),
        texto_extraido=texto,
    )
    db.add(ev)
    await db.flush()
    return ev


async def _confirm_link(db: AsyncSession, evidencia_id: uuid.UUID, obligacion_id: uuid.UUID) -> None:
    from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion, FuenteEnlace

    db.add(
        EvidenciaObligacion(
            evidencia_id=evidencia_id,
            obligacion_id=obligacion_id,
            confianza="alta",
            status=EstadoEnlace.CONFIRMED.value,
            source=FuenteEnlace.AI.value,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_contexto_evidencias_agrupa_por_obligacion_sin_fuga_cruzada(db: AsyncSession) -> None:
    """justification-context: Per-obligación context consumption — an obligación
    with confirmed links gets EXACTLY those evidencias, never another
    obligación's evidence (no cross-obligación leakage)."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    ob1 = await _make_obligacion(db, contrato.id, "Elaborar informes tecnicos mensuales", orden=1)
    ob2 = await _make_obligacion(db, contrato.id, "Asistir a reuniones de seguimiento", orden=2)
    ev1 = await _make_actividad_con_evidencia(db, cuenta.id, "informe.txt", "Contenido del informe mensual.")
    ev2 = await _make_actividad_con_evidencia(db, cuenta.id, "acta.txt", "Acta de la reunión de seguimiento.")
    await _confirm_link(db, ev1.id, ob1.id)
    await _confirm_link(db, ev2.id, ob2.id)
    await db.commit()

    contexto = await cuenta_cobro_service._contexto_evidencias_por_obligacion(db, cuenta.id, [ob1, ob2])

    assert "informe.txt" in contexto
    assert "Contenido del informe mensual." in contexto
    assert "acta.txt" in contexto
    assert "Acta de la reunión de seguimiento." in contexto
    # No cross-obligación leakage: ob1's section must not repeat ob2's evidence and vice versa.
    ob1_section = contexto.split(ob2.descripcion[:30])[0]
    assert "acta.txt" not in ob1_section


@pytest.mark.asyncio
async def test_contexto_evidencias_sin_ningun_enlace_usa_fallback_plano(db: AsyncSession) -> None:
    """justification-context: Flat blob as fallback only when no links exist —
    a cuenta with ZERO confirmed evidencia<->obligación links anywhere still
    gets context from the legacy flat top-10/1500-char query, so generation
    can proceed."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    ob1 = await _make_obligacion(db, contrato.id, "Elaborar informes tecnicos mensuales", orden=1)
    await _make_actividad_con_evidencia(db, cuenta.id, "suelto.txt", "Evidencia sin ningún enlace confirmado.")
    await db.commit()

    contexto = await cuenta_cobro_service._contexto_evidencias_por_obligacion(db, cuenta.id, [ob1])

    assert "suelto.txt" in contexto
    assert "Evidencia sin ningún enlace confirmado." in contexto


@pytest.mark.asyncio
async def test_contexto_evidencias_usa_cap_nuevo_no_el_plano(db: AsyncSession) -> None:
    """justification-context: Per-obligación context not bound to the old flat
    caps — 15 confirmed links for ONE obligación must be capped at the NEW
    per-obligación budget (4 evidencias / 1200 chars each), not the legacy flat
    cap (10 rows / 1500 chars)."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    ob1 = await _make_obligacion(db, contrato.id, "Elaborar informes tecnicos mensuales", orden=1)

    texto_largo = "X" * 2000  # longer than both the old (1500) and new (1200) char caps
    for i in range(15):
        ev = await _make_actividad_con_evidencia(db, cuenta.id, f"doc{i}.txt", texto_largo)
        await _confirm_link(db, ev.id, ob1.id)
    await db.commit()

    contexto = await cuenta_cobro_service._contexto_evidencias_por_obligacion(db, cuenta.id, [ob1])

    # Capped at 4 files, NOT the old flat 10-row cap.
    assert contexto.count(".txt") == 4
    # Each row truncated at the NEW 1200-char cap, not the old 1500.
    assert "X" * 1200 in contexto
    assert "X" * 1201 not in contexto
