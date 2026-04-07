"""Auth API endpoint tests."""

import pytest
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
async def test_change_password_success(client: AsyncClient) -> None:
    """User can change their password and then login with the new one."""
    reg_payload = {
        "email": "changepw@example.com",
        "password": "OldPass123!",
        "nombre": "ChangePW User",
    }
    await client.post("/api/v1/auth/register", json=reg_payload)

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "changepw@example.com", "password": "OldPass123!"},
    )
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    change_resp = await client.post(
        "/api/v1/auth/me/change-password",
        headers=headers,
        json={"current_password": "OldPass123!", "new_password": "NewPass456!"},
    )
    assert change_resp.status_code == 204

    # Old password should now be rejected
    old_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "changepw@example.com", "password": "OldPass123!"},
    )
    assert old_resp.status_code == 401

    # New password should work
    new_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "changepw@example.com", "password": "NewPass456!"},
    )
    assert new_resp.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current(client: AsyncClient, test_user: dict) -> None:
    """Providing the wrong current password should fail."""
    response = await client.post(
        "/api/v1/auth/me/change-password",
        headers=test_user["headers"],
        json={"current_password": "WrongCurrent!", "new_password": "NewPass456!"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_change_password_too_short(client: AsyncClient, test_user: dict) -> None:
    """New password shorter than 8 chars should be rejected with 422."""
    response = await client.post(
        "/api/v1/auth/me/change-password",
        headers=test_user["headers"],
        json={"current_password": "TestPass123!", "new_password": "short"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_me(client: AsyncClient, test_user: dict) -> None:
    """User can delete their own account and can no longer authenticate after."""
    # Account works before deletion
    me_resp = await client.get("/api/v1/auth/me", headers=test_user["headers"])
    assert me_resp.status_code == 200

    # Delete account
    del_resp = await client.delete("/api/v1/auth/me", headers=test_user["headers"])
    assert del_resp.status_code == 204

    # Token should be rejected after deletion
    me_resp2 = await client.get("/api/v1/auth/me", headers=test_user["headers"])
    assert me_resp2.status_code == 401


@pytest.mark.asyncio
async def test_401_has_www_authenticate_header(client: AsyncClient) -> None:
    """401 responses must include WWW-Authenticate: Bearer."""
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


# ---------- Admin /users endpoints ----------


async def _make_admin(client: AsyncClient) -> dict:
    """Register a user, promote them to admin in the DB, return headers."""
    from app.core.security import create_access_token
    from app.models.usuario import RolUsuario, Usuario
    from sqlalchemy import select

    from tests.conftest import async_session_test

    payload = {
        "email": "admin@example.com",
        "password": "AdminPass1!",
        "nombre": "Admin User",
    }
    await client.post("/api/v1/auth/register", json=payload)

    # Promote to admin directly in the test DB
    async with async_session_test() as session:
        result = await session.execute(select(Usuario).where(Usuario.email == "admin@example.com"))
        user = result.scalar_one()
        user.rol = RolUsuario.ADMIN
        await session.commit()
        user_id = str(user.id)

    admin_token = create_access_token(user_id, RolUsuario.ADMIN.value)
    return {"headers": {"Authorization": f"Bearer {admin_token}"}, "user_id": user_id}


@pytest.mark.asyncio
async def test_list_users_admin(client: AsyncClient, test_user: dict) -> None:
    """Admin can list all users."""
    admin = await _make_admin(client)
    response = await client.get("/api/v1/users/", headers=admin["headers"])
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_list_users_non_admin_forbidden(client: AsyncClient, test_user: dict) -> None:
    """Non-admin cannot list users."""
    response = await client.get("/api/v1/users/", headers=test_user["headers"])
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_deactivate_user(client: AsyncClient, test_user: dict) -> None:
    """Admin can deactivate a user; deactivated user cannot login."""
    admin = await _make_admin(client)
    user_id = str(test_user["user"].id)

    patch_resp = await client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin["headers"],
        json={"activo": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["activo"] is False

    # Deactivated user should not be able to use their token
    me_resp = await client.get("/api/v1/auth/me", headers=test_user["headers"])
    assert me_resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_reset_lockout(client: AsyncClient, test_user: dict) -> None:
    """Admin can reset the failed login attempts counter."""

    admin = await _make_admin(client)
    user_id = str(test_user["user"].id)

    patch_resp = await client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin["headers"],
        json={"reset_failed_attempts": True},
    )
    assert patch_resp.status_code == 200


@pytest.mark.asyncio
async def test_failed_login_attempts_persist_via_api(client: AsyncClient) -> None:
    """Failed login via the API must increment the counter in the DB (regression test)."""
    from app.models.usuario import Usuario
    from sqlalchemy import select

    reg = {
        "email": "failcount@example.com",
        "password": "StrongPass1!",
        "nombre": "Fail Count",
    }
    await client.post("/api/v1/auth/register", json=reg)

    # Two bad-password attempts through the API
    for _ in range(2):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "failcount@example.com", "password": "Wrong!"},
        )
        assert resp.status_code == 401

    # Verify counter in DB using test session
    from tests.conftest import async_session_test

    async with async_session_test() as session:
        result = await session.execute(select(Usuario).where(Usuario.email == "failcount@example.com"))
        user = result.scalar_one()
        assert user.failed_login_attempts == 2
