"""Google OAuth tests — service layer + API endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnauthorizedError
from app.models.usuario import Usuario
from app.schemas.auth import RegisterRequest
from app.services import auth_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _google_claims(
    *,
    uid: str = "google-uid-123",
    email: str = "google@example.com",
    name: str = "Google User",
    picture: str = "https://lh3.googleusercontent.com/a/photo.jpg",
) -> dict:
    return {"uid": uid, "email": email, "name": name, "picture": picture}


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_auth_creates_new_user(db: AsyncSession) -> None:
    """First Google sign-in creates a new user with provider='google'."""
    claims = _google_claims()
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        tokens = await auth_service.google_auth(db, "fake-id-token")

    assert tokens.access_token
    assert tokens.refresh_token

    result = await db.execute(select(Usuario).where(Usuario.email == "google@example.com"))
    user = result.scalar_one()
    assert user.google_id == "google-uid-123"
    assert user.provider == "google"
    assert user.password_hash is None
    assert user.photo_url == "https://lh3.googleusercontent.com/a/photo.jpg"
    assert user.nombre == "Google User"
    assert user.creditos_disponibles == 30


@pytest.mark.asyncio
async def test_google_auth_returns_tokens_on_second_login(db: AsyncSession) -> None:
    """Signing in a second time returns new tokens without duplicating the user."""
    claims = _google_claims()
    mock = AsyncMock(return_value=claims)
    with patch("app.core.firebase_admin.verify_firebase_token", new=mock):
        await auth_service.google_auth(db, "token-1")
        tokens2 = await auth_service.google_auth(db, "token-2")

    assert tokens2.access_token

    result = await db.execute(
        select(Usuario).where(Usuario.google_id == "google-uid-123")
    )
    users = result.scalars().all()
    assert len(users) == 1


@pytest.mark.asyncio
async def test_google_auth_links_existing_email_account(db: AsyncSession) -> None:
    """If an email account already exists, google_auth links the google_id to it."""
    # Create email-password account first
    reg = RegisterRequest(
        email="linked@example.com",
        password="StrongPass1!",
        nombre="Linked User",
        cedula="20000001",
    )
    await auth_service.register(db, reg)

    claims = _google_claims(uid="uid-link", email="linked@example.com", name="Linked User")
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        tokens = await auth_service.google_auth(db, "link-token")

    assert tokens.access_token

    result = await db.execute(select(Usuario).where(Usuario.email == "linked@example.com"))
    user = result.scalar_one()
    assert user.google_id == "uid-link"
    # Still has password (not wiped)
    assert user.password_hash is not None


@pytest.mark.asyncio
async def test_google_auth_updates_photo_on_existing_user(db: AsyncSession) -> None:
    """Photo URL is refreshed on every sign-in."""
    claims_first = _google_claims(picture="https://photo.old/img.jpg")
    claims_second = _google_claims(picture="https://photo.new/img.jpg")

    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims_first),
    ):
        await auth_service.google_auth(db, "t1")

    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims_second),
    ):
        await auth_service.google_auth(db, "t2")

    result = await db.execute(select(Usuario).where(Usuario.email == "google@example.com"))
    user = result.scalar_one()
    assert user.photo_url == "https://photo.new/img.jpg"


@pytest.mark.asyncio
async def test_google_auth_invalid_token_raises_unauthorized(db: AsyncSession) -> None:
    """Invalid Firebase token raises UnauthorizedError."""
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(side_effect=Exception("Token expired")),
    ):
        with pytest.raises(UnauthorizedError, match="Invalid or expired"):
            await auth_service.google_auth(db, "bad-token")


@pytest.mark.asyncio
async def test_google_auth_no_email_raises_unauthorized(db: AsyncSession) -> None:
    """Claims without email field raise UnauthorizedError."""
    claims = {"uid": "uid-noemail"}  # no email
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        with pytest.raises(UnauthorizedError, match="email"):
            await auth_service.google_auth(db, "no-email-token")


@pytest.mark.asyncio
async def test_google_auth_disabled_account_raises_unauthorized(db: AsyncSession) -> None:
    """Google auth on a disabled account raises UnauthorizedError."""
    # First create via Google
    claims = _google_claims(uid="uid-disabled", email="disabled@example.com")
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        await auth_service.google_auth(db, "first-token")

    # Disable the account
    result = await db.execute(select(Usuario).where(Usuario.email == "disabled@example.com"))
    user = result.scalar_one()
    user.activo = False
    await db.flush()

    # Try to sign in again
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        with pytest.raises(UnauthorizedError, match="disabled"):
            await auth_service.google_auth(db, "second-token")


@pytest.mark.asyncio
async def test_login_with_google_only_account_raises(db: AsyncSession) -> None:
    """Email+password login on a Google-only account raises UnauthorizedError."""
    claims = _google_claims(uid="uid-googonly", email="googonly@example.com")
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        await auth_service.google_auth(db, "setup-token")

    with pytest.raises(UnauthorizedError, match="Google Sign-in"):
        await auth_service.login(db, "googonly@example.com", "any-password")


@pytest.mark.asyncio
async def test_google_auth_name_fallback_to_email_prefix(db: AsyncSession) -> None:
    """When Google claims have no 'name', use email prefix as nombre."""
    claims = {"uid": "uid-noname", "email": "noname@example.com"}  # no 'name' or 'picture'
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        await auth_service.google_auth(db, "noname-token")

    result = await db.execute(select(Usuario).where(Usuario.email == "noname@example.com"))
    user = result.scalar_one()
    assert user.nombre == "noname"  # email prefix


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_google_login_new_user(client: AsyncClient) -> None:
    """POST /auth/google creates a new user and returns JWT tokens."""
    claims = _google_claims(uid="api-uid-1", email="api_google@example.com")
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        response = await client.post("/api/v1/auth/google", json={"id_token": "tok"})

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_api_google_login_invalid_token_returns_401(client: AsyncClient) -> None:
    """POST /auth/google with an invalid token returns 401."""
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(side_effect=Exception("bad token")),
    ):
        response = await client.post("/api/v1/auth/google", json={"id_token": "garbage"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_google_login_empty_token_returns_422(client: AsyncClient) -> None:
    """POST /auth/google with empty id_token fails schema validation."""
    response = await client.post("/api/v1/auth/google", json={"id_token": ""})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_google_login_missing_body_returns_422(client: AsyncClient) -> None:
    """POST /auth/google with missing body fails schema validation."""
    response = await client.post("/api/v1/auth/google", json={})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_google_login_returns_correct_user_on_me(client: AsyncClient) -> None:
    """After Google login, GET /auth/me returns correct user data."""
    claims = _google_claims(uid="api-uid-me", email="api_me@example.com", name="Me User")
    with patch(
        "app.core.firebase_admin.verify_firebase_token",
        new=AsyncMock(return_value=claims),
    ):
        login_resp = await client.post("/api/v1/auth/google", json={"id_token": "tok"})

    token = login_resp.json()["access_token"]
    me_resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me_resp.status_code == 200
    data = me_resp.json()
    assert data["email"] == "api_me@example.com"
    assert data["nombre"] == "Me User"
    assert data["provider"] == "google"
    assert data["photo_url"] == "https://lh3.googleusercontent.com/a/photo.jpg"


@pytest.mark.asyncio
async def test_api_google_login_idempotent(client: AsyncClient) -> None:
    """Calling /auth/google twice with same Google uid returns tokens both times without errors."""
    claims = _google_claims(uid="api-idem", email="idem@example.com")
    mock = AsyncMock(return_value=claims)
    with patch("app.core.firebase_admin.verify_firebase_token", new=mock):
        r1 = await client.post("/api/v1/auth/google", json={"id_token": "t1"})
        r2 = await client.post("/api/v1/auth/google", json={"id_token": "t2"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Different tokens each time (new JTI)
    assert r1.json()["access_token"] != r2.json()["access_token"]
