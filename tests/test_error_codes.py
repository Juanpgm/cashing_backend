"""Tests for the structured `code` field on the error envelope (additive to `detail`).

Guards three frontend recovery paths that previously relied on sniffing free
text / status codes:
- ACTIVIDADES_MISSING: checklist autogen fails because the cuenta has no Actividad rows.
- GOOGLE_NOT_CONNECTED: evidence discovery fails because Google isn't connected.
- CHECKLIST_INCOMPLETE: radicar_cuenta fails because mandatory requisitos are pending.

Also guards the regression case: an error without an assigned code keeps the
exact previous envelope shape (`code` is `None`, `detail` unchanged).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ERRCODE-001",
        objeto="Servicios profesionales para pruebas de error codes",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_sin_actividades(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=6,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def cuenta_para_radicar(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


async def test_checklist_autogen_sin_actividades_returns_actividades_missing_code(
    client: AsyncClient, test_user: dict[str, Any], cuenta_sin_actividades: CuentaCobro
) -> None:
    with patch(_PATCH_S3, return_value=_fake_storage()):
        resp = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta_sin_actividades.id}/checklist/INFORME_ACTIVIDADES/generar",
            headers=test_user["headers"],
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "ACTIVIDADES_MISSING"
    assert "actividades" in body["detail"].lower()


async def test_radicar_checklist_incompleto_returns_checklist_incomplete_code(
    client: AsyncClient, test_user: dict[str, Any], cuenta_para_radicar: CuentaCobro
) -> None:
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_para_radicar.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code in (400, 422), resp.text
    body = resp.json()
    assert body["code"] == "CHECKLIST_INCOMPLETE"


async def test_evidencias_descubrir_google_not_connected_returns_code(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    disconnected = AsyncMock()
    disconnected.connected = False

    with patch(
        "app.services.evidence_discovery_service.gws.get_integration_status",
        AsyncMock(return_value=disconnected),
    ):
        resp = await client.post(
            "/api/v1/integraciones/evidencias/descubrir",
            headers=test_user["headers"],
            json={
                "obligaciones": [{"descripcion": "Asistir a reuniones"}],
                "fecha_inicio": "2024-04-01",
                "fecha_fin": "2024-04-30",
            },
        )
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["code"] == "GOOGLE_NOT_CONNECTED"


async def test_unrelated_error_keeps_unchanged_envelope_shape(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    """Regression guard: an error without an assigned code has an unchanged envelope.

    `code` is additive and `None` when not set; `detail` and `trace_id` keep
    their prior meaning/format.
    """
    resp = await client.get(
        f"/api/v1/cuentas-cobro/{uuid.uuid4()}",
        headers=test_user["headers"],
    )
    assert resp.status_code == 404
    body = resp.json()
    assert set(body.keys()) == {"detail", "code", "trace_id"}
    assert body["code"] is None
    assert isinstance(body["detail"], str)
    assert "not found" in body["detail"].lower()
