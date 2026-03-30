"""Auth service unit tests."""

import pytest
from app.core.exceptions import AlreadyExistsError, NotFoundError, UnauthorizedError
from app.schemas.auth import RegisterRequest, UpdateUserRequest
from app.services import auth_service
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_register_creates_user(db: AsyncSession) -> None:
    data = RegisterRequest(
        email="svc@example.com",
        password="StrongPass1!",
        nombre="Svc User",
        cedula="555555555",
        telefono="+573005555555",
    )
    user = await auth_service.register(db, data)
    assert user.email == "svc@example.com"
    assert user.creditos_disponibles == 30


@pytest.mark.asyncio
async def test_register_duplicate_raises(db: AsyncSession) -> None:
    data = RegisterRequest(
        email="dup_svc@example.com",
        password="StrongPass1!",
        nombre="Dup User",
        cedula="666666666",
        telefono="+573006666666",
    )
    await auth_service.register(db, data)
    with pytest.raises(AlreadyExistsError):
        data2 = RegisterRequest(
            email="dup_svc@example.com",
            password="StrongPass1!",
            nombre="Dup User 2",
            cedula="777777777",
            telefono="+573007777777",
        )
        await auth_service.register(db, data2)


@pytest.mark.asyncio
async def test_login_wrong_password_raises(db: AsyncSession) -> None:
    data = RegisterRequest(
        email="login_svc@example.com",
        password="StrongPass1!",
        nombre="Login Svc",
        cedula="888888888",
        telefono="+573008888888",
    )
    await auth_service.register(db, data)
    with pytest.raises(UnauthorizedError):
        await auth_service.login(db, email="login_svc@example.com", password="WrongPass!")


@pytest.mark.asyncio
async def test_login_success(db: AsyncSession) -> None:
    data = RegisterRequest(
        email="ok_svc@example.com",
        password="StrongPass1!",
        nombre="Ok Svc",
        cedula="999999999",
        telefono="+573009999999",
    )
    await auth_service.register(db, data)
    tokens = await auth_service.login(db, email="ok_svc@example.com", password="StrongPass1!")
    assert tokens.access_token
    assert tokens.refresh_token


@pytest.mark.asyncio
async def test_login_increments_failed_attempts(db: AsyncSession) -> None:
    """Failed login should increment failed_login_attempts."""
    from app.models.usuario import Usuario
    from sqlalchemy import select

    data = RegisterRequest(
        email="fail_svc@example.com",
        password="StrongPass1!",
        nombre="Fail Svc",
    )
    await auth_service.register(db, data)

    with pytest.raises(UnauthorizedError):
        await auth_service.login(db, email="fail_svc@example.com", password="Wrong!")

    result = await db.execute(select(Usuario).where(Usuario.email == "fail_svc@example.com"))
    user = result.scalar_one()
    assert user.failed_login_attempts == 1


@pytest.mark.asyncio
async def test_login_resets_failed_attempts_on_success(db: AsyncSession) -> None:
    """Successful login should reset failed_login_attempts to 0."""
    from app.models.usuario import Usuario
    from sqlalchemy import select

    data = RegisterRequest(
        email="reset_svc@example.com",
        password="StrongPass1!",
        nombre="Reset Svc",
    )
    await auth_service.register(db, data)

    # Fail once
    with pytest.raises(UnauthorizedError):
        await auth_service.login(db, email="reset_svc@example.com", password="Wrong!")

    # Succeed
    await auth_service.login(db, email="reset_svc@example.com", password="StrongPass1!")

    result = await db.execute(select(Usuario).where(Usuario.email == "reset_svc@example.com"))
    user = result.scalar_one()
    assert user.failed_login_attempts == 0


@pytest.mark.asyncio
async def test_account_locked_after_10_failed_attempts(db: AsyncSession) -> None:
    """Account should be locked after 10 failed login attempts."""
    from app.models.usuario import Usuario
    from sqlalchemy import select

    data = RegisterRequest(
        email="locked_svc@example.com",
        password="StrongPass1!",
        nombre="Locked Svc",
    )
    await auth_service.register(db, data)

    # Simulate 10 failed attempts by setting directly
    result = await db.execute(select(Usuario).where(Usuario.email == "locked_svc@example.com"))
    user = result.scalar_one()
    user.failed_login_attempts = 10
    await db.flush()

    # Even correct password should fail
    with pytest.raises(UnauthorizedError, match="locked"):
        await auth_service.login(db, email="locked_svc@example.com", password="StrongPass1!")


@pytest.mark.asyncio
async def test_login_inactive_user(db: AsyncSession) -> None:
    """Inactive users should not be able to login."""
    from app.models.usuario import Usuario
    from sqlalchemy import select

    data = RegisterRequest(
        email="inactive_svc@example.com",
        password="StrongPass1!",
        nombre="Inactive Svc",
    )
    await auth_service.register(db, data)

    result = await db.execute(select(Usuario).where(Usuario.email == "inactive_svc@example.com"))
    user = result.scalar_one()
    user.activo = False
    await db.flush()

    with pytest.raises(UnauthorizedError):
        await auth_service.login(db, email="inactive_svc@example.com", password="StrongPass1!")


@pytest.mark.asyncio
async def test_get_user_by_id(db: AsyncSession) -> None:
    data = RegisterRequest(
        email="getuser@example.com",
        password="StrongPass1!",
        nombre="Get User",
    )
    user_resp = await auth_service.register(db, data)
    fetched = await auth_service.get_user_by_id(db, user_resp.id)
    assert fetched.email == "getuser@example.com"


@pytest.mark.asyncio
async def test_get_user_by_id_not_found(db: AsyncSession) -> None:
    import uuid
    with pytest.raises(NotFoundError):
        await auth_service.get_user_by_id(db, uuid.uuid4())


@pytest.mark.asyncio
async def test_update_user(db: AsyncSession) -> None:
    data = RegisterRequest(
        email="update_svc@example.com",
        password="StrongPass1!",
        nombre="Original Name",
    )
    user_resp = await auth_service.register(db, data)
    updated = await auth_service.update_user(
        db, user_resp.id, UpdateUserRequest(nombre="New Name")
    )
    assert updated.nombre == "New Name"
    assert updated.email == "update_svc@example.com"  # unchanged


@pytest.mark.asyncio
async def test_update_user_not_found(db: AsyncSession) -> None:
    import uuid
    with pytest.raises(NotFoundError):
        await auth_service.update_user(
            db, uuid.uuid4(), UpdateUserRequest(nombre="X")
        )


@pytest.mark.asyncio
async def test_refresh_tokens(db: AsyncSession) -> None:
    """Refresh tokens should return a new token pair."""
    data = RegisterRequest(
        email="refresh_svc@example.com",
        password="StrongPass1!",
        nombre="Refresh Svc",
    )
    await auth_service.register(db, data)
    tokens = await auth_service.login(db, email="refresh_svc@example.com", password="StrongPass1!")

    new_tokens = await auth_service.refresh_tokens(db, tokens.refresh_token)
    assert new_tokens.access_token
    assert new_tokens.refresh_token
    assert new_tokens.access_token != tokens.access_token


@pytest.mark.asyncio
async def test_refresh_with_invalid_token(db: AsyncSession) -> None:
    with pytest.raises(UnauthorizedError):
        await auth_service.refresh_tokens(db, "invalid.token.string")


@pytest.mark.asyncio
async def test_refresh_with_access_token_fails(db: AsyncSession) -> None:
    """Access tokens should not be usable as refresh tokens."""
    from app.core.security import create_access_token

    data = RegisterRequest(
        email="wrongtype@example.com",
        password="StrongPass1!",
        nombre="Wrong Type",
    )
    user_resp = await auth_service.register(db, data)
    access_token = create_access_token(str(user_resp.id), "contratista")

    with pytest.raises(UnauthorizedError):
        await auth_service.refresh_tokens(db, access_token)


@pytest.mark.asyncio
async def test_logout(db: AsyncSession) -> None:
    """Logout should blacklist the token."""
    from app.core.security import create_access_token

    data = RegisterRequest(
        email="logout_svc@example.com",
        password="StrongPass1!",
        nombre="Logout Svc",
    )
    user_resp = await auth_service.register(db, data)
    token = create_access_token(str(user_resp.id), "contratista")

    await auth_service.logout(db, token)

    # Verify token is blacklisted by trying to use it as refresh (tests DB entry)
    from app.core.security import decode_token
    from app.models.token_blacklist import TokenBlacklist
    from sqlalchemy import select

    payload = decode_token(token)
    result = await db.execute(select(TokenBlacklist).where(TokenBlacklist.jti == payload["jti"]))
    assert result.scalar_one_or_none() is not None
