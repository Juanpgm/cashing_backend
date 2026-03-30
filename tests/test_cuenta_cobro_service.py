"""CuentaCobro service unit tests."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.schemas.cuenta_cobro import ActividadCreate, CuentaCobroCreate
from app.services import cuenta_cobro_service


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


# ---------------------------------------------------------------------------
# eliminar_cuenta_cobro
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_borrador(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.BORRADOR)
    await db.commit()

    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id)
    await db.commit()

    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta.id)


@pytest.mark.asyncio
async def test_eliminar_cuenta_cobro_no_borrador_falla(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id, estado=EstadoCuentaCobro.ENVIADA)
    await db.commit()

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta.id)


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
