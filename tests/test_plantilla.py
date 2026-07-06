"""Tests for plantilla service and API."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plantilla import Plantilla, TipoPlantilla
from app.schemas.plantilla import PlantillaCreate, PlantillaUpdate
from app.services import plantilla_service

pytestmark = pytest.mark.asyncio

_HTML = "<html><body><h1>Cuenta de {{ entidad }}</h1></body></html>"


# ── Service tests ──────────────────────────────────────────────────────────────


async def test_crear_plantilla(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    data = PlantillaCreate(nombre="Mi Plantilla", tipo=TipoPlantilla.CUENTA_COBRO, contenido_html=_HTML)
    result = await plantilla_service.crear_plantilla(db, user.id, data)
    assert result.nombre == "Mi Plantilla"
    assert result.tipo == TipoPlantilla.CUENTA_COBRO
    assert result.activa is True


async def test_listar_plantillas(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    for i in range(3):
        p = Plantilla(
            usuario_id=user.id,
            nombre=f"Plantilla {i}",
            tipo=TipoPlantilla.INFORME_ACTIVIDADES,
            contenido_html=_HTML,
            activa=True,
        )
        db.add(p)
    await db.commit()

    result = await plantilla_service.listar_plantillas(db, user.id)
    assert len(result) == 3


async def test_listar_plantillas_filtrado_por_tipo(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    for tipo in [TipoPlantilla.CUENTA_COBRO, TipoPlantilla.INFORME_ACTIVIDADES]:
        db.add(Plantilla(usuario_id=user.id, nombre=f"P {tipo}", tipo=tipo, contenido_html=_HTML, activa=True))
    await db.commit()

    result = await plantilla_service.listar_plantillas(db, user.id, tipo=TipoPlantilla.CUENTA_COBRO)
    assert all(p.tipo == TipoPlantilla.CUENTA_COBRO for p in result)
    assert len(result) == 1


async def test_obtener_plantilla(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.core.exceptions import NotFoundError

    user = test_user["user"]
    p = Plantilla(usuario_id=user.id, nombre="Test", tipo=TipoPlantilla.CUENTA_COBRO, contenido_html=_HTML, activa=True)
    db.add(p)
    await db.commit()
    await db.refresh(p)

    result = await plantilla_service.obtener_plantilla(db, user.id, p.id)
    assert result.id == p.id

    with pytest.raises(NotFoundError):
        await plantilla_service.obtener_plantilla(db, user.id, uuid.uuid4())


async def test_actualizar_plantilla(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    p = Plantilla(usuario_id=user.id, nombre="Old", tipo=TipoPlantilla.CUENTA_COBRO, contenido_html=_HTML, activa=True)
    db.add(p)
    await db.commit()
    await db.refresh(p)

    result = await plantilla_service.actualizar_plantilla(
        db, user.id, p.id, PlantillaUpdate(nombre="New")
    )
    assert result.nombre == "New"


async def test_eliminar_plantilla(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    p = Plantilla(usuario_id=user.id, nombre="ToDelete", tipo=TipoPlantilla.CUENTA_COBRO, contenido_html=_HTML, activa=True)
    db.add(p)
    await db.commit()
    await db.refresh(p)

    await plantilla_service.eliminar_plantilla(db, user.id, p.id)
    # Should not appear in list after soft-delete
    lista = await plantilla_service.listar_plantillas(db, user.id)
    assert all(item.id != p.id for item in lista)


async def test_renderizar_plantilla(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.schemas.plantilla import PlantillaRenderRequest

    user = test_user["user"]
    p = Plantilla(
        usuario_id=user.id,
        nombre="Render",
        tipo=TipoPlantilla.CUENTA_COBRO,
        contenido_html=_HTML,
        activa=True,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)

    req = PlantillaRenderRequest(data={"entidad": "MinTIC"})
    result = await plantilla_service.renderizar_plantilla(db, user.id, p.id, req)
    assert "MinTIC" in result.html


# ── API tests ──────────────────────────────────────────────────────────────────


async def test_api_crear_plantilla(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.post(
        "/api/v1/plantillas/",
        headers=test_user["headers"],
        json={"nombre": "API Test", "tipo": "cuenta_cobro", "contenido_html": _HTML},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["nombre"] == "API Test"
    assert body["tipo"] == "cuenta_cobro"


async def test_api_listar_plantillas(client: AsyncClient, test_user: dict[str, Any]) -> None:
    # Create one first
    await client.post(
        "/api/v1/plantillas/",
        headers=test_user["headers"],
        json={"nombre": "Lista", "tipo": "informe_actividades", "contenido_html": _HTML},
    )
    resp = await client.get("/api/v1/plantillas/", headers=test_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


async def test_api_obtener_plantilla(client: AsyncClient, test_user: dict[str, Any]) -> None:
    create_resp = await client.post(
        "/api/v1/plantillas/",
        headers=test_user["headers"],
        json={"nombre": "GetMe", "tipo": "cuenta_cobro", "contenido_html": _HTML},
    )
    pid = create_resp.json()["id"]
    resp = await client.get(f"/api/v1/plantillas/{pid}", headers=test_user["headers"])
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


async def test_api_renderizar_plantilla(client: AsyncClient, test_user: dict[str, Any]) -> None:
    create_resp = await client.post(
        "/api/v1/plantillas/",
        headers=test_user["headers"],
        json={"nombre": "Render API", "tipo": "cuenta_cobro", "contenido_html": _HTML},
    )
    pid = create_resp.json()["id"]
    resp = await client.post(
        f"/api/v1/plantillas/{pid}/render",
        headers=test_user["headers"],
        json={"data": {"entidad": "SENA"}},
    )
    assert resp.status_code == 200
    assert "SENA" in resp.json()["html"]


async def test_api_eliminar_plantilla(client: AsyncClient, test_user: dict[str, Any]) -> None:
    create_resp = await client.post(
        "/api/v1/plantillas/",
        headers=test_user["headers"],
        json={"nombre": "DeleteMe", "tipo": "cuenta_cobro", "contenido_html": _HTML},
    )
    pid = create_resp.json()["id"]
    resp = await client.delete(f"/api/v1/plantillas/{pid}", headers=test_user["headers"])
    assert resp.status_code == 204
    # GET should return 404 now
    resp2 = await client.get(f"/api/v1/plantillas/{pid}", headers=test_user["headers"])
    assert resp2.status_code == 404


async def test_api_no_autorizado(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/plantillas/")
    assert resp.status_code in (401, 403)
