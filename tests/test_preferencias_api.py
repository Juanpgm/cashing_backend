"""API tests for GET/PATCH /api/v1/usuarios/preferencias (Phase 7)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_get_preferencias_returns_defaults_when_no_row_exists(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    """A user with no preferencias_usuario rows yet must get defaults, never a 404."""
    response = await client.get("/api/v1/usuarios/preferencias", headers=test_user["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "notificaciones_email": True,
        "idioma": "es",
        "timezone": "America/Bogota",
    }


async def test_get_preferencias_returns_existing_values(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession
) -> None:
    """Stored rows override defaults; keys without a row still fall back to defaults."""
    from app.models.preferencia_usuario import PreferenciaUsuario

    db.add(
        PreferenciaUsuario(
            usuario_id=test_user["user"].id,
            clave="idioma",
            valor="en",
        )
    )
    await db.commit()

    response = await client.get("/api/v1/usuarios/preferencias", headers=test_user["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data["idioma"] == "en"
    assert data["notificaciones_email"] is True
    assert data["timezone"] == "America/Bogota"


async def test_patch_preferencias_partial_update(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    """PATCH with a single field only touches that field; others keep their current value."""
    response = await client.patch(
        "/api/v1/usuarios/preferencias",
        json={"notificaciones_email": False},
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert data["notificaciones_email"] is False
    assert data["idioma"] == "es"
    assert data["timezone"] == "America/Bogota"

    # A second partial update must not clobber the first change.
    response2 = await client.patch(
        "/api/v1/usuarios/preferencias",
        json={"idioma": "en"},
        headers=test_user["headers"],
    )
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["idioma"] == "en"
    assert data2["notificaciones_email"] is False


async def test_patch_preferencias_invalid_idioma_is_rejected(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    response = await client.patch(
        "/api/v1/usuarios/preferencias",
        json={"idioma": "fr"},
        headers=test_user["headers"],
    )
    assert response.status_code == 422


async def test_patch_preferencias_invalid_timezone_is_rejected(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    response = await client.patch(
        "/api/v1/usuarios/preferencias",
        json={"timezone": "Not/AZone"},
        headers=test_user["headers"],
    )
    assert response.status_code == 422


async def test_get_preferencias_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/usuarios/preferencias")
    assert response.status_code == 401


async def test_patch_preferencias_requires_auth(client: AsyncClient) -> None:
    response = await client.patch(
        "/api/v1/usuarios/preferencias", json={"idioma": "en"}
    )
    assert response.status_code == 401
