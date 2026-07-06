"""Integration test for POST /integraciones/evidencias/descubrir."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.google_workspace import (
    EvidenceDiscoveryResponse,
    ObligacionJustificada,
)


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

    with patch(
        "app.api.v1.integraciones.eds.descubrir_evidencias",
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
