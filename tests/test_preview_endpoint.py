"""Tests for the cuenta cobro preview and borradores endpoints (Phase 5)."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato_with_cuenta(db: AsyncSession, test_user: dict[str, Any]):
    from datetime import date
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PREV-001",
        objeto="Test preview contrato para el agente",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Entidad Test",
        supervisor_nombre="Supervisor Test",
    )
    db.add(c)
    await db.flush()
    cc = CuentaCobro(
        contrato_id=c.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def cuenta_with_borrador(db: AsyncSession, contrato_with_cuenta: CuentaCobro):
    borrador = BorradorCuentaCobro(
        cuenta_cobro_id=contrato_with_cuenta.id,
        version=1,
        contenido={"html": "<h1>Cuenta de cobro</h1>", "texto": "Test"},
    )
    db.add(borrador)
    await db.commit()
    return contrato_with_cuenta


async def test_preview_returns_404_no_borrador(
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato_with_cuenta: CuentaCobro,
) -> None:
    response = await client.get(
        f"/api/v1/cuentas-cobro/{contrato_with_cuenta.id}/preview",
        headers=test_user["headers"],
    )
    assert response.status_code == 404


async def test_preview_returns_html_with_borrador(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_with_borrador: CuentaCobro,
) -> None:
    response = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_with_borrador.id}/preview",
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


async def test_preview_requires_auth(
    client: AsyncClient,
    contrato_with_cuenta: CuentaCobro,
) -> None:
    response = await client.get(
        f"/api/v1/cuentas-cobro/{contrato_with_cuenta.id}/preview"
    )
    assert response.status_code == 401


async def test_preview_nonexistent_cuenta(
    client: AsyncClient,
    test_user: dict[str, Any],
) -> None:
    fake_id = str(uuid.uuid4())
    response = await client.get(
        f"/api/v1/cuentas-cobro/{fake_id}/preview",
        headers=test_user["headers"],
    )
    assert response.status_code == 404


async def test_list_borradores_empty(
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato_with_cuenta: CuentaCobro,
) -> None:
    response = await client.get(
        f"/api/v1/cuentas-cobro/{contrato_with_cuenta.id}/borradores",
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_list_borradores_returns_versions(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_with_borrador: CuentaCobro,
) -> None:
    response = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_with_borrador.id}/borradores",
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1

