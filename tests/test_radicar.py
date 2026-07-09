"""Tests for POST /cuentas-cobro/{id}/radicar (B.3).

Radicar gates the borrador->enviada transition on checklist readiness: it
delegates the actual state change to the existing `cambiar_estado` state
machine, but first refuses when mandatory requisitos are still pending.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.tools.invoke import invoke_tool as real_invoke_tool
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

# Mandatory (obligatorio=True) codes in the standard catalog seed — see
# checklist_service._CATALOGO_SEED. Marking all of these cumplido_manual
# satisfies computar_resumen's radicacion_lista gate.
_CODIGOS_OBLIGATORIOS = [
    "CONTRATO",
    "RPC",
    "SEGURIDAD_SOCIAL",
    "INFORME_ACTIVIDADES",
    "INFORME_SUPERVISION",
    "EVIDENCIAS",
    "CEDULA",
    "RUT",
    "ACTA_INICIO",
]


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-RADICAR-001",
        objeto="Servicios profesionales para pruebas de radicación",
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
async def cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    """Cuenta in BORRADOR with the checklist gate already resolved (estandar)."""
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


async def _completar_checklist(client: AsyncClient, headers: dict[str, str], cuenta_id: uuid.UUID) -> None:
    """Seed the checklist rows and mark every mandatory requisito as cumplido_manual."""
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist", headers=headers)
    assert r.status_code == 200, r.text

    for codigo in _CODIGOS_OBLIGATORIOS:
        p = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/{codigo}",
            headers=headers,
            json={"cumplido_manual": True},
        )
        assert p.status_code == 200, p.text


async def test_radicar_checklist_incompleto_400(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code in (400, 422), resp.text
    body = resp.json()
    # Pending requisito labels/codes must be surfaced so the user knows what's missing.
    assert "CONTRATO" in body["detail"] or "pendiente" in body["detail"].lower()


async def test_radicar_checklist_completo_200(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estado"] == "enviada"
    assert body["fecha_envio"] is not None


async def test_radicar_cuenta_ya_enviada_400(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    first = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert second.status_code in (400, 422)


async def test_radicar_cuenta_de_otro_usuario_404(
    client: AsyncClient, db: AsyncSession, cuenta: CuentaCobro
) -> None:
    from app.core.security import create_access_token, hash_password
    from app.models.usuario import Usuario

    otro = Usuario(
        email="otro-radicar@example.com",
        nombre="Otro Usuario",
        cedula="987654321",
        password_hash=hash_password("OtroPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro)
    await db.commit()
    await db.refresh(otro)
    token = create_access_token(subject=str(otro.id), role=otro.rol)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


async def test_radicar_cuenta_inexistente_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{uuid.uuid4()}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 404


async def test_radicar_desde_rechazada_200(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, cuenta: CuentaCobro
) -> None:
    """Radicar is also allowed to resubmit a RECHAZADA cuenta directly to ENVIADA."""
    cuenta.estado = EstadoCuentaCobro.RECHAZADA
    db.add(cuenta)
    await db.commit()

    await _completar_checklist(client, test_user["headers"], cuenta.id)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["estado"] == "enviada"


async def test_radicar_cuenta_routes_through_tool_registry(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /cuentas-cobro/{id}/radicar must dispatch through
    `invoke_tool("radicar_cuenta", ...)` — the shared tool registry (same handler
    the /mcp server exposes) — rather than calling `cuenta_cobro_service.radicar_cuenta`
    directly. The spy wraps the real `invoke_tool` so the happy-path response and
    checklist gating stay unchanged; it only records the call for assertion.
    """
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    spy = AsyncMock(side_effect=real_invoke_tool)
    monkeypatch.setattr("app.api.v1.cuentas_cobro.invoke_tool", spy)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["estado"] == "enviada"
    spy.assert_awaited_once()
    assert spy.await_args is not None
    assert spy.await_args.args[0] == "radicar_cuenta"
