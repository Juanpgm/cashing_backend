"""Authentication service — register, login, refresh, user management."""

import uuid
from datetime import UTC, datetime

from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AlreadyExistsError, NotFoundError, UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.credito import Credito, TipoCredito
from app.models.token_blacklist import TokenBlacklist
from app.models.usuario import RolUsuario, Usuario
from app.schemas.auth import (
    RegisterRequest,
    TokenResponse,
    UpdateUserRequest,
    UserResponse,
)


async def register(db: AsyncSession, data: RegisterRequest) -> UserResponse:
    """Register a new user."""
    result = await db.execute(select(Usuario).where(Usuario.email == data.email))
    if result.scalar_one_or_none() is not None:
        raise AlreadyExistsError("Usuario", "email")

    user = Usuario(
        email=data.email,
        nombre=data.nombre,
        cedula=data.cedula,
        telefono=data.telefono,
        password_hash=hash_password(data.password),
        rol=RolUsuario.CONTRATISTA,
        creditos_disponibles=settings.FREE_CREDITS_ON_SIGNUP,
    )
    db.add(user)
    await db.flush()

    # Register signup bonus credits
    credit = Credito(
        usuario_id=user.id,
        cantidad=settings.FREE_CREDITS_ON_SIGNUP,
        tipo=TipoCredito.BONUS,
        referencia="signup_bonus",
    )
    db.add(credit)
    await db.flush()

    return UserResponse.model_validate(user)


async def login(db: AsyncSession, email: str, password: str) -> TokenResponse:
    """Authenticate user and return JWT tokens."""
    result = await db.execute(select(Usuario).where(Usuario.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        raise UnauthorizedError("Invalid email or password")

    if not user.activo:
        raise UnauthorizedError("Account is disabled")

    if user.failed_login_attempts >= 10:
        raise UnauthorizedError("Account locked due to too many failed attempts")

    if not verify_password(password, user.password_hash):
        user.failed_login_attempts += 1
        await db.flush()
        raise UnauthorizedError("Invalid email or password")

    # Reset failed attempts on success
    user.failed_login_attempts = 0
    await db.flush()

    access_token = create_access_token(str(user.id), user.rol.value)
    refresh_token = create_refresh_token(str(user.id))

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


async def refresh_tokens(db: AsyncSession, refresh_token_str: str) -> TokenResponse:
    """Refresh tokens: validate old refresh, blacklist it, issue new pair."""
    try:
        payload = decode_token(refresh_token_str)
    except JWTError:
        raise UnauthorizedError("Invalid refresh token") from None

    if payload.get("type") != "refresh":
        raise UnauthorizedError("Invalid token type")

    jti = payload.get("jti", "")

    # Check if token is blacklisted
    result = await db.execute(select(TokenBlacklist).where(TokenBlacklist.jti == jti))
    if result.scalar_one_or_none() is not None:
        raise UnauthorizedError("Token has been revoked")

    # Blacklist the old refresh token
    exp_str = payload.get("exp", "")
    expires_at = datetime.fromtimestamp(float(exp_str), tz=UTC) if exp_str else datetime.now(UTC)
    blacklist_entry = TokenBlacklist(jti=jti, expires_at=expires_at)
    db.add(blacklist_entry)
    await db.flush()

    user_id = payload.get("sub", "")
    result = await db.execute(select(Usuario).where(Usuario.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()

    if user is None or not user.activo:
        raise UnauthorizedError("User not found or inactive")

    access_token = create_access_token(str(user.id), user.rol.value)
    new_refresh = create_refresh_token(str(user.id))

    return TokenResponse(access_token=access_token, refresh_token=new_refresh)


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> UserResponse:
    """Get user by ID."""
    result = await db.execute(select(Usuario).where(Usuario.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("Usuario", str(user_id))
    return UserResponse.model_validate(user)


async def update_user(
    db: AsyncSession, user_id: uuid.UUID, data: UpdateUserRequest
) -> UserResponse:
    """Update user profile fields."""
    result = await db.execute(select(Usuario).where(Usuario.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("Usuario", str(user_id))

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)
    await db.flush()

    return UserResponse.model_validate(user)


async def logout(db: AsyncSession, token: str) -> None:
    """Blacklist the current access token so it can't be reused."""
    try:
        payload = decode_token(token)
    except JWTError:
        raise UnauthorizedError("Invalid token") from None

    jti = payload.get("jti", "")
    if not jti:
        raise UnauthorizedError("Invalid token: missing jti")

    # Check if already blacklisted
    result = await db.execute(select(TokenBlacklist).where(TokenBlacklist.jti == jti))
    if result.scalar_one_or_none() is not None:
        return  # Already revoked, no-op

    exp_str = payload.get("exp", "")
    expires_at = datetime.fromtimestamp(float(exp_str), tz=UTC) if exp_str else datetime.now(UTC)
    blacklist_entry = TokenBlacklist(jti=jti, expires_at=expires_at)
    db.add(blacklist_entry)
    await db.flush()
