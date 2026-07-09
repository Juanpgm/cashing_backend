"""Unit tests for app.core.auth.authenticate_bearer — the reusable bearer-token
authentication core shared by app.api.deps.get_current_user and (later) the MCP
server. No FastAPI dependency machinery involved here — just token in, Usuario out.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.core.auth import authenticate_bearer
from app.core.config import settings
from app.core.exceptions import UnauthorizedError
from app.core.security import create_access_token, create_refresh_token, decode_token, hash_password
from app.models.token_blacklist import TokenBlacklist
from app.models.usuario import Usuario
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_user(db: AsyncSession, **overrides: object) -> Usuario:
    defaults: dict[str, object] = {
        "email": "core_auth@example.com",
        "nombre": "Core Auth User",
        "cedula": "10101010",
        "password_hash": hash_password("StrongPass1!"),
        "rol": "contratista",
        "activo": True,
        "creditos_disponibles": 10,
    }
    defaults.update(overrides)
    user = Usuario(**defaults)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_authenticate_bearer_valid_token(db: AsyncSession) -> None:
    user = await _make_user(db)
    token = create_access_token(subject=str(user.id), role=user.rol)

    authenticated = await authenticate_bearer(token, db)

    assert authenticated.id == user.id
    assert authenticated.email == "core_auth@example.com"


@pytest.mark.asyncio
async def test_authenticate_bearer_garbage_token(db: AsyncSession) -> None:
    with pytest.raises(UnauthorizedError, match="Invalid or expired token"):
        await authenticate_bearer("not.a.valid.jwt", db)


@pytest.mark.asyncio
async def test_authenticate_bearer_expired_token(db: AsyncSession) -> None:
    user = await _make_user(db, email="expired@example.com", cedula="10101011")
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "exp": now - timedelta(minutes=5),
        "iat": now - timedelta(minutes=10),
        "jti": "expired-jti",
        "rol": user.rol,
        "type": "access",
    }
    expired_token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(UnauthorizedError, match="Invalid or expired token"):
        await authenticate_bearer(expired_token, db)


@pytest.mark.asyncio
async def test_authenticate_bearer_wrong_token_type_refresh(db: AsyncSession) -> None:
    user = await _make_user(db, email="refresh_type@example.com", cedula="10101012")
    refresh_token = create_refresh_token(subject=str(user.id))

    with pytest.raises(UnauthorizedError, match="Invalid token type"):
        await authenticate_bearer(refresh_token, db)


@pytest.mark.asyncio
async def test_authenticate_bearer_blacklisted_jti(db: AsyncSession) -> None:
    user = await _make_user(db, email="blacklisted@example.com", cedula="10101013")
    token = create_access_token(subject=str(user.id), role=user.rol)
    payload = decode_token(token)

    db.add(
        TokenBlacklist(
            jti=payload["jti"],
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await db.commit()

    with pytest.raises(UnauthorizedError, match="Token has been revoked"):
        await authenticate_bearer(token, db)


@pytest.mark.asyncio
async def test_authenticate_bearer_inactive_user(db: AsyncSession) -> None:
    user = await _make_user(db, email="inactive@example.com", cedula="10101014", activo=False)
    token = create_access_token(subject=str(user.id), role=user.rol)

    with pytest.raises(UnauthorizedError, match="User not found or inactive"):
        await authenticate_bearer(token, db)


@pytest.mark.asyncio
async def test_authenticate_bearer_deleted_user(db: AsyncSession) -> None:
    user = await _make_user(db, email="deleted@example.com", cedula="10101015")
    user.soft_delete()
    await db.commit()
    token = create_access_token(subject=str(user.id), role=user.rol)

    with pytest.raises(UnauthorizedError, match="User not found or inactive"):
        await authenticate_bearer(token, db)


@pytest.mark.asyncio
async def test_authenticate_bearer_unknown_user(db: AsyncSession) -> None:
    import uuid

    token = create_access_token(subject=str(uuid.uuid4()), role="contratista")

    with pytest.raises(UnauthorizedError, match="User not found or inactive"):
        await authenticate_bearer(token, db)
