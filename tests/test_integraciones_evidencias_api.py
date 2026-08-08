"""Integration test for POST /integraciones/evidencias/descubrir."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.schemas.google_workspace import (
    EvidenceDiscoveryResponse,
    ObligacionJustificada,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_descubrir_endpoint_requires_auth(client):
    resp = await client.post(
        "/api/v1/integraciones/evidencias/descubrir",
        json={"obligaciones": [{"descripcion": "x"}], "fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_descubrir_endpoint_returns_justifications(client, test_user):
    canned = EvidenceDiscoveryResponse(
        obligaciones=[
            ObligacionJustificada(
                obligacion_id="ob1",
                descripcion="Entregar informe mensual",
                justificacion="Entregué el informe mensual de actividades.",
                evidencias=[
                    {"source": "email", "titulo": "Informe", "link": "https://mail.google.com/x", "fecha": "2024-04-10"}
                ],
            )
        ],
        resumen="Encontré 1 evidencia.",
        total_evidencias=1,
        fuentes={"email": 1, "drive": 0, "calendar": 0},
    )

    # Patched at the service module (the single real implementation both the
    # endpoint and the /mcp "descubrir_evidencias" tool call through
    # `invoke_tool`), not at an endpoint-local alias — the endpoint no longer
    # imports the service directly, it dispatches via the shared tool registry.
    with patch(
        "app.services.evidence_discovery_service.descubrir_evidencias",
        AsyncMock(return_value=canned),
    ):
        resp = await client.post(
            "/api/v1/integraciones/evidencias/descubrir",
            headers=test_user["headers"],
            json={
                "obligaciones": [{"id": "ob1", "descripcion": "Entregar informe mensual"}],
                "fecha_inicio": "2024-04-01",
                "fecha_fin": "2024-04-30",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_evidencias"] == 1
    assert data["obligaciones"][0]["obligacion_id"] == "ob1"
    assert data["obligaciones"][0]["evidencias"][0]["link"].startswith("https://mail.google.com")


async def _make_contrato_and_cuenta(db: AsyncSession, usuario_id) -> Any:
    from datetime import date

    from app.models.contrato import Contrato
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro

    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="CTR-API-CACHE-001",
        objeto="Prestación de servicios",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 2, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1_000_000)
    db.add(cuenta)
    await db.commit()
    return cuenta


@pytest.mark.asyncio
async def test_descubrir_endpoint_refresh_true_bypasses_warm_cache(
    client, test_user: dict[str, Any], db: AsyncSession
) -> None:
    """Slice 5a.3: `refresh=true` bypasses a warm discovery-cache entry and
    repopulates it; `refresh` omitted/false serves the cached value — both routed
    through the real service (invoke_tool), not a canned mock."""
    from app.services import discovery_cache
    from app.services import evidence_discovery_service as eds

    discovery_cache.clear()
    cuenta = await _make_contrato_and_cuenta(db, test_user["user"].id)

    gmail = MagicMock()
    gmail.search_messages = AsyncMock(return_value=[])
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(return_value=MagicMock(content="No hay evidencia."))
    from app.models.integracion import IntegrationProvider

    connected_status = MagicMock()
    connected_status.connected = True
    connected_status.provider = IntegrationProvider.GOOGLE
    gmail_ctor = MagicMock(return_value=gmail)

    payload = {
        "obligaciones": [{"id": "ob1", "descripcion": "Entregar informe mensual"}],
        "cuenta_id": str(cuenta.id),
        "fecha_inicio": "2024-04-01",
        "fecha_fin": "2024-04-30",
    }

    with (
        patch.object(eds.integration_service, "has_any_connected_provider", AsyncMock(return_value=True)),
        patch.object(eds.integration_service, "list_integration_statuses", AsyncMock(return_value=[connected_status])),
        patch.object(eds, "GmailAdapter", gmail_ctor),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        r1 = await client.post("/api/v1/integraciones/evidencias/descubrir", headers=test_user["headers"], json=payload)
        r2 = await client.post("/api/v1/integraciones/evidencias/descubrir", headers=test_user["headers"], json=payload)
        r3 = await client.post(
            "/api/v1/integraciones/evidencias/descubrir",
            headers=test_user["headers"],
            json={**payload, "refresh": True},
        )

    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert gmail_ctor.call_count == 2  # r1 (miss) + r3 (refresh) — r2 served from cache
