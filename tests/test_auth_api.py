"""Auth API endpoint tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient) -> None:
    payload = {
        "email": "new@example.com",
        "password": "StrongPass1!",
        "nombre": "Nuevo User",
        "cedula": "987654321",
        "telefono": "+573009876543",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "new@example.com"
    assert data["nombre"] == "Nuevo User"
    assert "id" in data
    assert data["creditos_disponibles"] == 30  # FREE_CREDITS_ON_SIGNUP


@pytest.mark.asyncio
async def test_register_duplicate_email(client: AsyncClient) -> None:
    payload = {
        "email": "dup@example.com",
        "password": "StrongPass1!",
        "nombre": "First User",
        "cedula": "111111111",
        "telefono": "+573001111111",
    }
    response1 = await client.post("/api/v1/auth/register", json=payload)
    assert response1.status_code == 201

    response2 = await client.post("/api/v1/auth/register", json=payload)
    assert response2.status_code == 409


@pytest.mark.asyncio
async def test_register_short_password(client: AsyncClient) -> None:
    payload = {
        "email": "short@example.com",
        "password": "abc",
        "nombre": "Short Pass",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_email(client: AsyncClient) -> None:
    payload = {
        "email": "not-an-email",
        "password": "StrongPass1!",
        "nombre": "Invalid Email",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient) -> None:
    # Register first
    payload = {
        "email": "login@example.com",
        "password": "StrongPass1!",
        "nombre": "Login User",
        "cedula": "222222222",
        "telefono": "+573002222222",
    }
    await client.post("/api/v1/auth/register", json=payload)

    # Login
    login_payload = {"email": "login@example.com", "password": "StrongPass1!"}
    response = await client.post("/api/v1/auth/login", json=login_payload)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient) -> None:
    payload = {
        "email": "wrong@example.com",
        "password": "StrongPass1!",
        "nombre": "Wrong User",
        "cedula": "333333333",
        "telefono": "+573003333333",
    }
    await client.post("/api/v1/auth/register", json=payload)

    login_payload = {"email": "wrong@example.com", "password": "WrongPass!"}
    response = await client.post("/api/v1/auth/login", json=login_payload)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: AsyncClient) -> None:
    login_payload = {"email": "noexist@example.com", "password": "Whatever1!"}
    response = await client.post("/api/v1/auth/login", json=login_payload)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me(client: AsyncClient, test_user: dict) -> None:
    response = await client.get("/api/v1/auth/me", headers=test_user["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["nombre"] == "Test User"
    assert data["rol"] == "contratista"


@pytest.mark.asyncio
async def test_get_me_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_invalid_token(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token(client: AsyncClient) -> None:
    # Register first
    payload = {
        "email": "refresh@example.com",
        "password": "StrongPass1!",
        "nombre": "Refresh User",
        "cedula": "444444444",
        "telefono": "+573004444444",
    }
    await client.post("/api/v1/auth/register", json=payload)

    # Login to get tokens
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "refresh@example.com", "password": "StrongPass1!"},
    )
    refresh_token = login_resp.json()["refresh_token"]

    # Use refresh token
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_refresh_token_reuse_blocked(client: AsyncClient) -> None:
    """A refresh token can only be used once — reuse should fail."""
    payload = {
        "email": "reuse@example.com",
        "password": "StrongPass1!",
        "nombre": "Reuse User",
        "cedula": "20000001",
    }
    await client.post("/api/v1/auth/register", json=payload)
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "reuse@example.com", "password": "StrongPass1!"},
    )
    refresh_token = login_resp.json()["refresh_token"]

    # First refresh succeeds
    resp1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp1.status_code == 200

    # Second refresh with same token fails (blacklisted)
    resp2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp2.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_access_token_fails(client: AsyncClient, test_user: dict) -> None:
    """Using an access token as a refresh token should fail."""
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": test_user["token"]},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_me(client: AsyncClient, test_user: dict) -> None:
    response = await client.put(
        "/api/v1/auth/me",
        headers=test_user["headers"],
        json={"nombre": "Updated Name"},
    )
    assert response.status_code == 200
    assert response.json()["nombre"] == "Updated Name"


@pytest.mark.asyncio
async def test_update_me_partial(client: AsyncClient, test_user: dict) -> None:
    """Partial update should only change the specified fields."""
    response = await client.put(
        "/api/v1/auth/me",
        headers=test_user["headers"],
        json={"telefono": "+573009999999"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["telefono"] == "+573009999999"
    assert data["nombre"] == "Test User"  # unchanged


@pytest.mark.asyncio
async def test_logout(client: AsyncClient, test_user: dict) -> None:
    """Logout should blacklist the access token."""
    # Verify token works before logout
    me_resp = await client.get("/api/v1/auth/me", headers=test_user["headers"])
    assert me_resp.status_code == 200

    # Logout
    logout_resp = await client.post("/api/v1/auth/logout", headers=test_user["headers"])
    assert logout_resp.status_code == 204

    # Token should be revoked now
    me_resp2 = await client.get("/api/v1/auth/me", headers=test_user["headers"])
    assert me_resp2.status_code == 401


@pytest.mark.asyncio
async def test_logout_unauthorized(client: AsyncClient) -> None:
    """Logout without a token should fail."""
    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_me_new_cedula_triggers_secop_import(client: AsyncClient, db) -> None:
    """Setting cedula for the first time triggers SECOP contract import."""
    from app.core.security import create_access_token, hash_password
    from app.models.usuario import Usuario

    user = Usuario(
        email="nocedula@example.com",
        nombre="No Cedula User",
        cedula=None,
        password_hash=hash_password("TestPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=10,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(subject=str(user.id), role=user.rol)
    headers = {"Authorization": f"Bearer {token}"}

    mock_result = MagicMock(importados=3, actualizados=0, omitidos_duplicados=0)
    mock_import = AsyncMock(return_value=mock_result)

    with patch("app.services.secop_service.importar_contratos_secop", mock_import):
        response = await client.put(
            "/api/v1/auth/me",
            headers=headers,
            json={"cedula": "1016019452"},
        )

    assert response.status_code == 200
    assert response.json()["cedula"] == "1016019452"
    mock_import.assert_called_once()
    call_kwargs = mock_import.call_args.kwargs
    assert call_kwargs["documento_proveedor"] == "1016019452"
    assert call_kwargs["confirmar"] is True


@pytest.mark.asyncio
async def test_update_me_existing_cedula_no_reimport(client: AsyncClient, test_user: dict) -> None:
    """Updating a field other than cedula should NOT trigger SECOP import."""
    mock_import = AsyncMock()

    with patch("app.services.secop_service.importar_contratos_secop", mock_import):
        response = await client.put(
            "/api/v1/auth/me",
            headers=test_user["headers"],
            json={"nombre": "Updated Name"},
        )

    assert response.status_code == 200
    assert response.json()["nombre"] == "Updated Name"
    mock_import.assert_not_called()


@pytest.mark.asyncio
async def test_update_me_invalid_cedula_format(client: AsyncClient, test_user: dict) -> None:
    """Cedula with non-numeric or out-of-range characters should return 422."""
    response = await client.put(
        "/api/v1/auth/me",
        headers=test_user["headers"],
        json={"cedula": "abc123"},
    )
    assert response.status_code == 422
