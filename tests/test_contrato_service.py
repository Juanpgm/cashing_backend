"""Contrato service unit tests."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.schemas.contrato import (
    ContratoCreate,
    ContratoUpdate,
    ObligacionCreate,
)
from app.services import contrato_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_contrato_create(**overrides) -> ContratoCreate:
    defaults = {
        "numero_contrato": "CTR-2024-001",
        "objeto": "Prestación de servicios de consultoría tecnológica",
        "valor_total": "36000000.00",
        "valor_mensual": "3000000.00",
        "fecha_inicio": date(2024, 1, 1),
        "fecha_fin": date(2024, 12, 31),
        "supervisor_nombre": "Ana Supervisora",
        "entidad": "Ministerio de TIC",
        "dependencia": "Dirección de Sistemas",
        "obligaciones": [],
    }
    defaults.update(overrides)
    return ContratoCreate(**defaults)


# ---------------------------------------------------------------------------
# crear_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_contrato_basico(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create()
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    assert result.numero_contrato == "CTR-2024-001"
    assert result.obligaciones == []
    assert result.usuario_id == test_user["user"].id


@pytest.mark.asyncio
async def test_crear_contrato_con_obligaciones(db: AsyncSession, test_user: dict[str, Any]) -> None:
    obs = [
        ObligacionCreate(descripcion="Elaborar informes técnicos mensuales", tipo=TipoObligacion.ESPECIFICA, orden=1),
        ObligacionCreate(descripcion="Asistir a reuniones del equipo de trabajo", tipo=TipoObligacion.GENERAL, orden=2),
    ]
    data = _make_contrato_create(obligaciones=obs)
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    assert len(result.obligaciones) == 2
    assert result.obligaciones[0].orden == 1


@pytest.mark.asyncio
async def test_crear_contrato_fecha_invalida(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create(fecha_inicio=date(2024, 12, 31), fecha_fin=date(2024, 1, 1))
    with pytest.raises(ValidationError):
        await contrato_service.crear_contrato(db, test_user["user"].id, data)


@pytest.mark.asyncio
async def test_crear_contrato_fechas_iguales(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create(fecha_inicio=date(2024, 1, 1), fecha_fin=date(2024, 1, 1))
    with pytest.raises(ValidationError):
        await contrato_service.crear_contrato(db, test_user["user"].id, data)


# ---------------------------------------------------------------------------
# listar_contratos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listar_contratos_vacia(db: AsyncSession, test_user: dict[str, Any]) -> None:
    result = await contrato_service.listar_contratos(db, test_user["user"].id)
    assert result == []


@pytest.mark.asyncio
async def test_listar_contratos_solo_del_usuario(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create()
    await contrato_service.crear_contrato(db, test_user["user"].id, data)

    otro_usuario_id = uuid.uuid4()
    result = await contrato_service.listar_contratos(db, otro_usuario_id)
    assert result == []

    result_own = await contrato_service.listar_contratos(db, test_user["user"].id)
    assert len(result_own) == 1


# ---------------------------------------------------------------------------
# obtener_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obtener_contrato_existente(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    result = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert result.id == created.id


@pytest.mark.asyncio
async def test_obtener_contrato_no_encontrado(db: AsyncSession, test_user: dict[str, Any]) -> None:
    with pytest.raises(NotFoundError):
        await contrato_service.obtener_contrato(db, test_user["user"].id, uuid.uuid4())


@pytest.mark.asyncio
async def test_obtener_contrato_otro_usuario(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    with pytest.raises(NotFoundError):
        await contrato_service.obtener_contrato(db, uuid.uuid4(), created.id)


# ---------------------------------------------------------------------------
# actualizar_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actualizar_contrato_parcial(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    update = ContratoUpdate(entidad="Nueva Entidad")
    result = await contrato_service.actualizar_contrato(db, test_user["user"].id, created.id, update)
    assert result.entidad == "Nueva Entidad"
    assert result.numero_contrato == created.numero_contrato


@pytest.mark.asyncio
async def test_actualizar_contrato_fecha_invalida(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    update = ContratoUpdate(fecha_fin=date(2023, 1, 1))
    with pytest.raises(ValidationError):
        await contrato_service.actualizar_contrato(db, test_user["user"].id, created.id, update)


# ---------------------------------------------------------------------------
# eliminar_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_contrato_ok(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    await contrato_service.eliminar_contrato(db, test_user["user"].id, created.id)

    with pytest.raises(NotFoundError):
        await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)


@pytest.mark.asyncio
async def test_eliminar_contrato_bloqueado_por_cuenta_activa(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())

    cc = CuentaCobro(
        contrato_id=created.id,
        mes=1,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.ENVIADA,
    )
    db.add(cc)
    await db.commit()

    with pytest.raises(ValidationError):
        await contrato_service.eliminar_contrato(db, test_user["user"].id, created.id)


@pytest.mark.asyncio
async def test_eliminar_contrato_permite_cuenta_borrador(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())

    cc = CuentaCobro(
        contrato_id=created.id,
        mes=1,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cc)
    await db.commit()

    # Should not raise — borrador is not a blocking state
    await contrato_service.eliminar_contrato(db, test_user["user"].id, created.id)


# ---------------------------------------------------------------------------
# agregar_obligacion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agregar_obligacion_ok(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    ob_data = ObligacionCreate(
        descripcion="Elaborar informes técnicos mensuales de avance",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    result = await contrato_service.agregar_obligacion(db, test_user["user"].id, created.id, ob_data)
    assert result.descripcion == ob_data.descripcion
    assert result.contrato_id == created.id


@pytest.mark.asyncio
async def test_agregar_obligacion_contrato_no_encontrado(db: AsyncSession, test_user: dict[str, Any]) -> None:
    ob_data = ObligacionCreate(
        descripcion="Elaborar informes técnicos mensuales de avance",
        tipo=TipoObligacion.ESPECIFICA,
    )
    with pytest.raises(NotFoundError):
        await contrato_service.agregar_obligacion(db, test_user["user"].id, uuid.uuid4(), ob_data)


# ---------------------------------------------------------------------------
# eliminar_obligacion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_obligacion_ok(db: AsyncSession, test_user: dict[str, Any]) -> None:
    obs = [ObligacionCreate(descripcion="Asistir a reuniones del equipo de trabajo", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(
        db, test_user["user"].id, _make_contrato_create(obligaciones=obs)
    )
    ob_id = created.obligaciones[0].id
    await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, ob_id)

    refreshed = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert len(refreshed.obligaciones) == 0


@pytest.mark.asyncio
async def test_eliminar_obligacion_no_encontrada(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    with pytest.raises(NotFoundError):
        await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_eliminar_obligacion_bloqueada_por_actividad(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.models.actividad import Actividad

    obs = [ObligacionCreate(descripcion="Asistir a reuniones del equipo de trabajo", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(
        db, test_user["user"].id, _make_contrato_create(obligaciones=obs)
    )
    ob_id = created.obligaciones[0].id

    cc = CuentaCobro(
        contrato_id=created.id, mes=1, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR
    )
    db.add(cc)
    await db.flush()

    actividad = Actividad(
        cuenta_cobro_id=cc.id,
        obligacion_id=ob_id,
        descripcion="Actividad que referencia la obligación para el bloqueo",
    )
    db.add(actividad)
    await db.commit()

    with pytest.raises(ValidationError):
        await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, ob_id)
