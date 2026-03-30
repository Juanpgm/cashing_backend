"""CuentasCobro API integration tests."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import get_pdf_storage
from app.main import app as fastapi_app
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
async def cuenta_borrador(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


def _mock_pdf_storage() -> S3StorageAdapter:
    mock = AsyncMock(spec=S3StorageAdapter)
    mock.upload = AsyncMock(return_value="pdfs/fake/fake.pdf")
    mock.presigned_url = AsyncMock(return_value="https://storage.example.com/fake.pdf")
    return mock  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /cuentas-cobro — create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_201(client: AsyncClient, test_user: dict[str, Any], contrato: Contrato) -> None:
    payload = {"contrato_id": str(contrato.id), "mes": 1, "anio": 2024, "valor": "3000000.00"}
    resp = await client.post("/api/v1/cuentas-cobro/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 201
    data = resp.json()
    assert data["estado"] == "borrador"
    assert data["mes"] == 1
    assert data["anio"] == 2024
    assert "id" in data


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_sin_autenticacion(client: AsyncClient, contrato: Contrato) -> None:
    payload = {"contrato_id": str(contrato.id), "mes": 1, "anio": 2024, "valor": "3000000.00"}
    resp = await client.post("/api/v1/cuentas-cobro/", json=payload)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_contrato_inexistente(client: AsyncClient, test_user: dict[str, Any]) -> None:
    payload = {"contrato_id": str(uuid.uuid4()), "mes": 1, "anio": 2024, "valor": "3000000.00"}
    resp = await client.post("/api/v1/cuentas-cobro/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_duplicada_409(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    payload = {"contrato_id": str(contrato.id), "mes": 2, "anio": 2024, "valor": "3000000.00"}
    resp1 = await client.post("/api/v1/cuentas-cobro/", json=payload, headers=test_user["headers"])
    assert resp1.status_code == 201
    resp2 = await client.post("/api/v1/cuentas-cobro/", json=payload, headers=test_user["headers"])
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_mes_invalido(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    payload = {"contrato_id": str(contrato.id), "mes": 13, "anio": 2024, "valor": "3000000.00"}
    resp = await client.post("/api/v1/cuentas-cobro/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_sin_creditos(
    client: AsyncClient, contrato: Contrato, db: AsyncSession, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    user.creditos_disponibles = 0
    db.add(user)
    await db.commit()

    payload = {"contrato_id": str(contrato.id), "mes": 5, "anio": 2024, "valor": "3000000.00"}
    resp = await client.post("/api/v1/cuentas-cobro/", json=payload, headers=test_user["headers"])
    assert resp.status_code == 402


# ---------------------------------------------------------------------------
# GET /cuentas-cobro
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_vacia(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.get("/api/v1/cuentas-cobro/", headers=test_user["headers"])
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_con_datos(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    resp = await client.get("/api/v1/cuentas-cobro/", headers=test_user["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == str(cuenta_borrador.id)


# ---------------------------------------------------------------------------
# GET /cuentas-cobro/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obtener_cuenta_cobro_200(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta_borrador.id}", headers=test_user["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(cuenta_borrador.id)
    assert data["actividades"] == []


@pytest.mark.asyncio
async def test_obtener_cuenta_cobro_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.get(f"/api/v1/cuentas-cobro/{uuid.uuid4()}", headers=test_user["headers"])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /cuentas-cobro/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_cuenta_borrador_204(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    resp = await client.delete(f"/api/v1/cuentas-cobro/{cuenta_borrador.id}", headers=test_user["headers"])
    assert resp.status_code == 204

    # Should now 404
    resp2 = await client.get(f"/api/v1/cuentas-cobro/{cuenta_borrador.id}", headers=test_user["headers"])
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_eliminar_cuenta_no_borrador_422(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, db: AsyncSession
) -> None:
    cc = CuentaCobro(contrato_id=contrato.id, mes=6, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.ENVIADA)
    db.add(cc)
    await db.commit()

    resp = await client.delete(f"/api/v1/cuentas-cobro/{cc.id}", headers=test_user["headers"])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /cuentas-cobro/{id}/actividades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agregar_actividad_201(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    payload = {"descripcion": "Participé en reunión de seguimiento del proyecto con el equipo técnico"}
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/actividades",
        json=payload,
        headers=test_user["headers"],
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["descripcion"] == payload["descripcion"]
    assert "id" in data


@pytest.mark.asyncio
async def test_agregar_actividad_descripcion_muy_corta(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    payload = {"descripcion": "corta"}
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/actividades",
        json=payload,
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_agregar_actividad_estado_invalido(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, db: AsyncSession
) -> None:
    cc = CuentaCobro(contrato_id=contrato.id, mes=7, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.ENVIADA)
    db.add(cc)
    await db.commit()

    payload = {"descripcion": "Esta actividad no debería poder agregarse en estado enviada"}
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cc.id}/actividades",
        json=payload,
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /cuentas-cobro/{id}/estado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cambiar_estado_borrador_a_enviada(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/estado",
        json={"estado": "enviada"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["estado"] == "enviada"


@pytest.mark.asyncio
async def test_cambiar_estado_transicion_invalida_422(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/estado",
        json={"estado": "aprobada"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cambiar_estado_valor_invalido_422(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/estado",
        json={"estado": "estado_inventado"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /cuentas-cobro/{id}/generar-pdf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generar_pdf_200(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_pdf_storage

    try:
        with patch("app.services.cuenta_cobro_service.generate_pdf_from_html", return_value=b"%PDF-fake"):
            resp = await client.post(
                f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/generar-pdf",
                headers=test_user["headers"],
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 200
    data = resp.json()
    assert "pdf_url" in data
    assert "pdf_storage_key" in data


@pytest.mark.asyncio
async def test_generar_pdf_cuenta_no_encontrada(client: AsyncClient, test_user: dict[str, Any]) -> None:
    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_pdf_storage
    try:
        with patch("app.services.cuenta_cobro_service.generate_pdf_from_html", return_value=b"%PDF-fake"):
            resp = await client.post(
                f"/api/v1/cuentas-cobro/{uuid.uuid4()}/generar-pdf",
                headers=test_user["headers"],
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /cuentas-cobro/{id}/pdf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obtener_url_pdf_sin_generar_422(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro
) -> None:
    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_pdf_storage
    try:
        resp = await client.get(
            f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/pdf",
            headers=test_user["headers"],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_obtener_url_pdf_ok(
    client: AsyncClient, test_user: dict[str, Any], cuenta_borrador: CuentaCobro, db: AsyncSession
) -> None:
    cuenta_borrador.pdf_storage_key = "pdfs/fake/fake.pdf"
    db.add(cuenta_borrador)
    await db.commit()

    fastapi_app.dependency_overrides[get_pdf_storage] = _mock_pdf_storage
    try:
        resp = await client.get(
            f"/api/v1/cuentas-cobro/{cuenta_borrador.id}/pdf",
            headers=test_user["headers"],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_pdf_storage, None)

    assert resp.status_code == 200
    assert "pdf_url" in resp.json()
