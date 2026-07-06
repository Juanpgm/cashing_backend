"""Tests for actividad service and API."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.schemas.actividad import ActividadCreate, ActividadUpdate
from app.services import actividad_service

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ACT-001",
        objeto="Servicios de consultoría tecnológica",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Ana Supervisora",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=3_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── Service tests ──────────────────────────────────────────────────────────────


async def test_crear_actividad(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    user = test_user["user"]
    data = ActividadCreate(
        descripcion="Reunión de seguimiento con el equipo de desarrollo",
        fecha_realizacion=date(2024, 3, 15),
    )
    result = await actividad_service.crear_actividad(db, user.id, cuenta_cobro.id, data)
    assert result.descripcion == data.descripcion
    assert result.cuenta_cobro_id == cuenta_cobro.id


async def test_listar_actividades(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    user = test_user["user"]
    for i in range(3):
        await actividad_service.crear_actividad(
            db,
            user.id,
            cuenta_cobro.id,
            ActividadCreate(descripcion=f"Actividad {i}", fecha_realizacion=date(2024, 3, i + 1)),
        )
    result = await actividad_service.listar_actividades(db, user.id, cuenta_cobro.id)
    assert len(result) == 3


async def test_actualizar_actividad(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    user = test_user["user"]
    created = await actividad_service.crear_actividad(
        db, user.id, cuenta_cobro.id, ActividadCreate(descripcion="Reunión de seguimiento del proyecto")
    )
    updated = await actividad_service.actualizar_actividad(
        db, user.id, cuenta_cobro.id, created.id, ActividadUpdate(descripcion="Informe de actividades actualizado")
    )
    assert updated.descripcion == "Informe de actividades actualizado"


async def test_eliminar_actividad(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    user = test_user["user"]
    created = await actividad_service.crear_actividad(
        db, user.id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad a eliminar de prueba")
    )
    await actividad_service.eliminar_actividad(db, user.id, cuenta_cobro.id, created.id)
    lista = await actividad_service.listar_actividades(db, user.id, cuenta_cobro.id)
    assert all(a.id != created.id for a in lista)


async def test_crear_actividad_cuenta_cobro_enviada_falla(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    from app.core.exceptions import ValidationError

    user = test_user["user"]
    cuenta_cobro.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    with pytest.raises(ValidationError):
        await actividad_service.crear_actividad(
            db, user.id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad que debería estar bloqueada")
        )


async def test_crear_actividad_cuenta_cobro_not_found(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    from app.core.exceptions import NotFoundError

    user = test_user["user"]
    with pytest.raises(NotFoundError):
        await actividad_service.crear_actividad(
            db, user.id, uuid.uuid4(), ActividadCreate(descripcion="Actividad en cuenta cobro inexistente")
        )


# ── API tests ──────────────────────────────────────────────────────────────────


async def test_api_crear_actividad(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
        json={"descripcion": "Actividad de prueba para API", "fecha_realizacion": "2024-03-10"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["descripcion"] == "Actividad de prueba para API"


async def test_api_listar_actividades(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    # Create via API
    await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
        json={"descripcion": "Actividad para test de lista"},
    )
    resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
