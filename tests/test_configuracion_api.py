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
async def admin_user(db: AsyncSession) -> dict[str, Any]:
    """Admin-role counterpart to `test_user` — for `AdminUser`-gated endpoints."""
    user = Usuario(
        email="admin-config@example.com",
        nombre="Admin Config",
        cedula="000111222",
        telefono="+573000001111",
        password_hash=hash_password("AdminPass123!"),
        rol="admin",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(subject=str(user.id), role=user.rol)
    return {
        "user": user,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


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


@pytest.mark.asyncio
async def test_purgar_todo_admin_usuario_no_admin_403(client: AsyncClient, test_user: dict[str, Any]) -> None:
    """A regular `contratista` must never reach the global wipe — this is the
    admin-role enforcement gate itself, not just a UX detail."""
    resp = await client.post(
        "/api/v1/configuracion/purgar-todo-admin",
        headers=test_user["headers"],
        json={"confirmacion": "BORRAR TODO"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_purgar_todo_admin_sin_confirmacion_exacta_422(client: AsyncClient, admin_user: dict[str, Any]) -> None:
    resp = await client.post(
        "/api/v1/configuracion/purgar-todo-admin",
        headers=admin_user["headers"],
        json={"confirmacion": "BORRAR"},  # right text, wrong endpoint's confirmation
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_purgar_todo_admin_confirmado_200(
    client: AsyncClient, admin_user: dict[str, Any], contrato_con_cuenta: dict[str, Any], db: AsyncSession
) -> None:
    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_storage
    try:
        resp = await client.post(
            "/api/v1/configuracion/purgar-todo-admin",
            headers=admin_user["headers"],
            json={"confirmacion": "BORRAR TODO"},
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cuentas_borradas"] == 1

    # Global scope proof: test_user's cuenta (not the admin's own) is gone too.
    cuenta_id = contrato_con_cuenta["cuenta"].id
    result = await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))
    assert result.scalar_one_or_none() is None

    # No auto-lockout: the admin who ran the wipe can still be looked up.
    assert (await db.get(Usuario, admin_user["user"].id)) is not None
