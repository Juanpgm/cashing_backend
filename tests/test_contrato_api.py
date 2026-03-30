"""Contrato API integration tests."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import TipoObligacion


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BASE_PAYLOAD = {
    "numero_contrato": "CTR-2024-001",
    "objeto": "Prestación de servicios de consultoría tecnológica avanzada",
    "valor_total": "36000000.00",
    "valor_mensual": "3000000.00",
    "fecha_inicio": "2024-01-01",
    "fecha_fin": "2024-12-31",
    "supervisor_nombre": "Ana Supervisora",
    "entidad": "Ministerio de TIC",
    "dependencia": "Dirección de Sistemas",
}


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-001-2024",
        objeto="Prestación de servicios de consultoría tecnológica",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Ministerio de Tecnología",
        dependencia="Dirección de Sistemas",
        supervisor_nombre="Pedro Supervisor",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# POST /contratos — create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_contrato_201(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.post("/api/v1/contratos/", json=_BASE_PAYLOAD, headers=test_user["headers"])
    assert resp.status_code == 201
    data = resp.json()
    assert data["numero_contrato"] == "CTR-2024-001"
    assert data["obligaciones"] == []
    assert "id" in data


@pytest.mark.asyncio
async def test_crear_contrato_con_obligaciones_201(client: AsyncClient, test_user: dict[str, Any]) -> None:
    payload = {
        **_BASE_PAYLOAD,
        "obligaciones": [
            {"descripcion": "Elaborar informes técnicos mensuales de avance", "tipo": "especifica", "orden": 1},
            {"descripcion": "Asistir a reuniones del equipo de trabajo", "tipo": "general", "orden": 2},
        ],
    }
    resp = await client.post("/api/v1/contratos/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["obligaciones"]) == 2


@pytest.mark.asyncio
async def test_crear_contrato_sin_autenticacion(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/contratos/", json=_BASE_PAYLOAD)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_crear_contrato_fecha_invalida(client: AsyncClient, test_user: dict[str, Any]) -> None:
    payload = {**_BASE_PAYLOAD, "fecha_inicio": "2024-12-31", "fecha_fin": "2024-01-01"}
    resp = await client.post("/api/v1/contratos/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_crear_contrato_objeto_muy_corto(client: AsyncClient, test_user: dict[str, Any]) -> None:
    payload = {**_BASE_PAYLOAD, "objeto": "Corto"}
    resp = await client.post("/api/v1/contratos/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /contratos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listar_contratos_vacia(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.get("/api/v1/contratos/", headers=test_user["headers"])
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_listar_contratos_con_datos(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await client.get("/api/v1/contratos/", headers=test_user["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == str(contrato.id)


@pytest.mark.asyncio
async def test_listar_contratos_sin_autenticacion(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/contratos/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /contratos/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obtener_contrato_200(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await client.get(f"/api/v1/contratos/{contrato.id}", headers=test_user["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(contrato.id)
    assert data["obligaciones"] == []


@pytest.mark.asyncio
async def test_obtener_contrato_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.get(f"/api/v1/contratos/{uuid.uuid4()}", headers=test_user["headers"])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /contratos/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actualizar_contrato_200(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await client.patch(
        f"/api/v1/contratos/{contrato.id}",
        json={"entidad": "Nueva Entidad Actualizada"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["entidad"] == "Nueva Entidad Actualizada"


@pytest.mark.asyncio
async def test_actualizar_contrato_fecha_invalida(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await client.patch(
        f"/api/v1/contratos/{contrato.id}",
        json={"fecha_fin": "2023-01-01"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_actualizar_contrato_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.patch(
        f"/api/v1/contratos/{uuid.uuid4()}",
        json={"entidad": "X"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /contratos/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_contrato_204(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await client.delete(f"/api/v1/contratos/{contrato.id}", headers=test_user["headers"])
    assert resp.status_code == 204

    resp2 = await client.get(f"/api/v1/contratos/{contrato.id}", headers=test_user["headers"])
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_eliminar_contrato_bloqueado_por_cuenta_activa(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, db: AsyncSession
) -> None:
    cc = CuentaCobro(
        contrato_id=contrato.id, mes=1, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.ENVIADA
    )
    db.add(cc)
    await db.commit()

    resp = await client.delete(f"/api/v1/contratos/{contrato.id}", headers=test_user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_eliminar_contrato_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.delete(f"/api/v1/contratos/{uuid.uuid4()}", headers=test_user["headers"])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /contratos/{id}/obligaciones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agregar_obligacion_201(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    payload = {
        "descripcion": "Elaborar informes técnicos mensuales de avance del proyecto",
        "tipo": "especifica",
        "orden": 1,
    }
    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/obligaciones",
        json=payload,
        headers=test_user["headers"],
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["descripcion"] == payload["descripcion"]
    assert data["contrato_id"] == str(contrato.id)


@pytest.mark.asyncio
async def test_agregar_obligacion_descripcion_corta(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    payload = {"descripcion": "Ok", "tipo": "especifica"}
    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/obligaciones",
        json=payload,
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_agregar_obligacion_contrato_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    payload = {
        "descripcion": "Elaborar informes técnicos mensuales de avance del proyecto",
        "tipo": "especifica",
    }
    resp = await client.post(
        f"/api/v1/contratos/{uuid.uuid4()}/obligaciones",
        json=payload,
        headers=test_user["headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /contratos/{id}/obligaciones/{ob_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_obligacion_204(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, db: AsyncSession
) -> None:
    from app.models.obligacion import Obligacion

    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Asistir a reuniones del equipo de trabajo",
        tipo=TipoObligacion.GENERAL,
        orden=1,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)

    resp = await client.delete(
        f"/api/v1/contratos/{contrato.id}/obligaciones/{ob.id}",
        headers=test_user["headers"],
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_eliminar_obligacion_404(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await client.delete(
        f"/api/v1/contratos/{contrato.id}/obligaciones/{uuid.uuid4()}",
        headers=test_user["headers"],
    )
    assert resp.status_code == 404
