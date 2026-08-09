"""Configuración API integration tests (self-service purge test data)."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.api.deps import get_pdf_storage
from app.core.security import create_access_token, hash_password
from app.main import app as fastapi_app
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.usuario import Usuario
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=[])
    return storage


@pytest.fixture
async def contrato_con_cuenta(db: AsyncSession, test_user: dict[str, Any]) -> dict[str, Any]:
    contrato = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-CONFIG-001",
        objeto="Prestación de servicios de consultoría tecnológica",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Ministerio de Tecnología",
        dependencia="Dirección de Sistemas",
        supervisor_nombre="Pedro Supervisor",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)

    cuenta = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR)
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)
    return {"contrato": contrato, "cuenta": cuenta}


@pytest.mark.asyncio
async def test_purgar_datos_prueba_sin_confirmacion_422(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.post(
        "/api/v1/configuracion/purgar-datos-prueba",
        headers=test_user["headers"],
        json={"confirmacion": "borrar"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_purgar_datos_prueba_confirmado_200(
    client: AsyncClient, test_user: dict[str, Any], contrato_con_cuenta: dict[str, Any]
) -> None:
    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_storage
    try:
        resp = await client.post(
            "/api/v1/configuracion/purgar-datos-prueba",
            headers=test_user["headers"],
            json={"confirmacion": "BORRAR"},
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cuentas_borradas": 1, "obligaciones_reiniciadas": 0, "contratos_omitidos": 0}


@pytest.mark.asyncio
async def test_purgar_datos_prueba_no_afecta_otro_usuario(
    client: AsyncClient, test_user: dict[str, Any], contrato_con_cuenta: dict[str, Any], db: AsyncSession
) -> None:
    otro = Usuario(
        email="otro-purga@example.com",
        nombre="Otro Usuario",
        cedula="111222333",
        telefono="+573001112222",
        password_hash=hash_password("OtroPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro)
    await db.commit()
    await db.refresh(otro)
    otro_token = create_access_token(subject=str(otro.id), role=otro.rol)

    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_storage
    try:
        resp = await client.post(
            "/api/v1/configuracion/purgar-datos-prueba",
            headers={"Authorization": f"Bearer {otro_token}"},
            json={"confirmacion": "BORRAR"},
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cuentas_borradas": 0, "obligaciones_reiniciadas": 0, "contratos_omitidos": 0}

    # test_user's cuenta must survive untouched — another user's purge is scoped away from it.
    cuenta_id = contrato_con_cuenta["cuenta"].id
    result = await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))
    assert result.scalar_one_or_none() is not None
