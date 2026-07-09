"""Authentication service — register, login, refresh, Google OAuth, user management."""

import uuid
from datetime import UTC, datetime

import structlog
from jose import JWTError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AlreadyExistsError,
    InviteRequiredError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.credito import Credito, TipoCredito
from app.models.invite_code import InviteCode
from app.models.token_blacklist import TokenBlacklist
from app.models.usuario import RolUsuario, Usuario
from app.adapters.storage.port import StoragePort
from app.schemas.auth import (
    RegisterRequest,
    TokenResponse,
    UpdateUserRequest,
    UserResponse,
)

log = structlog.get_logger("service.auth")


async def _resolve_photo_url(photo_url: str | None) -> str | None:
    """Resolve an S3 key to a 7-day presigned URL; direct URLs (http/https) pass through unchanged."""
    if not photo_url or photo_url.startswith("http"):
        return photo_url
    try:
        from app.adapters.storage.s3_adapter import S3StorageAdapter
        storage = S3StorageAdapter(bucket=settings.S3_BUCKET_AVATARS)
        return await storage.presigned_url(photo_url, expires_in=604800)
    except Exception:  # noqa: BLE001
        log.warning("photo_url_resolve_failed", key=photo_url)
        return None


async def _consume_invite_code(db: AsyncSession, code: str | None) -> None:
    """Validate and consume one use of an invite code when the waitlist gate is on.

    No-op when ``WAITLIST_ENABLED`` is False. Otherwise raises ``InviteRequiredError``
    if the code is missing, unknown, inactive, or already exhausted. On success it
    increments the code's usage counter within the caller's transaction, so a later
    failure (e.g. duplicate email) rolls the consumption back atomically.
    """
    if not settings.WAITLIST_ENABLED:
        return

    if not code:
        raise InviteRequiredError()

    result = await db.execute(select(InviteCode).where(InviteCode.codigo == code))
    invite = result.scalar_one_or_none()
    if invite is None or not invite.disponible:
        raise InviteRequiredError("Código de invitación inválido o agotado.")

    invite.usos_actuales += 1
    await db.flush()


async def register(db: AsyncSession, data: RegisterRequest) -> UserResponse:
    """Register a new user and auto-import their SECOP contracts if cedula is provided."""
    result = await db.execute(select(Usuario).where(Usuario.email == data.email))
    if result.scalar_one_or_none() is not None:
        raise AlreadyExistsError("Usuario", "email")

    await _consume_invite_code(db, data.invite_code)

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

    # Auto-import SECOP contracts when cedula is present
    if data.cedula:
        try:
            from app.services.secop_service import importar_contratos_secop
            result_secop = await importar_contratos_secop(
                db=db,
                documento_proveedor=data.cedula,
                usuario_id=user.id,
                confirmar=True,
            )
            log.info(
                "register_secop_import",
                usuario_id=str(user.id),
                cedula=data.cedula,
                importados=result_secop.importados,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("register_secop_import_failed", cedula=data.cedula, error=str(exc))

    return UserResponse.model_validate(user)


async def login(db: AsyncSession, email: str, password: str) -> TokenResponse:
    """Authenticate user with email + password and return JWT tokens."""
    result = await db.execute(select(Usuario).where(Usuario.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        raise UnauthorizedError("Invalid email or password")

    if not user.activo:
        raise UnauthorizedError("Account is disabled")

    if user.failed_login_attempts >= 10:
        raise UnauthorizedError("Account locked due to too many failed attempts")

    # Google-only accounts have no password
    if user.password_hash is None:
        raise UnauthorizedError(
            "Esta cuenta usa Google Sign-in. Usá 'Iniciar sesión con Google'."
        )

    if not verify_password(password, user.password_hash):
        user.failed_login_attempts += 1
        await db.flush()
        raise UnauthorizedError("Invalid email or password")

    user.failed_login_attempts = 0
    await db.flush()

    access_token = create_access_token(str(user.id), user.rol.value)
    refresh_token = create_refresh_token(str(user.id))

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


async def google_auth(
    db: AsyncSession, id_token: str, invite_code: str | None = None
) -> TokenResponse:
    """Authenticate (or register) a user via Firebase Google Sign-in.

    Flow:
    1. Verify the Firebase ID token with firebase-admin.
    2. Find an existing user by google_id OR email (links accounts automatically).
    3. Create a new user if none found (upsert with signup bonus credits). New
       accounts pass through the waitlist gate; existing users log in unimpeded.
    4. Return our own JWT pair — same as email login, so the frontend is token-agnostic.
    """
    from app.core.firebase_admin import verify_firebase_token

    try:
        claims = await verify_firebase_token(id_token)
    except Exception as exc:
        log.warning("google_auth_token_invalid", error=str(exc))
        raise UnauthorizedError("Invalid or expired Google ID token") from None

    google_id: str = claims["uid"]
    email: str | None = claims.get("email")
    nombre: str = claims.get("name") or (email or "").split("@")[0]
    photo_url: str | None = claims.get("picture")

    if not email:
        raise UnauthorizedError("La cuenta de Google no tiene un email verificado.")

    # Find by google_id first, then fall back to email match
    result = await db.execute(
        select(Usuario).where(
            or_(Usuario.google_id == google_id, Usuario.email == email)
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        # First-time Google sign-in — gated account creation
        await _consume_invite_code(db, invite_code)
        user = Usuario(
            email=email,
            nombre=nombre,
            google_id=google_id,
            photo_url=photo_url,
            provider="google",
            password_hash=None,
            rol=RolUsuario.CONTRATISTA,
            creditos_disponibles=settings.FREE_CREDITS_ON_SIGNUP,
        )
        db.add(user)
        await db.flush()

        credit = Credito(
            usuario_id=user.id,
            cantidad=settings.FREE_CREDITS_ON_SIGNUP,
            tipo=TipoCredito.BONUS,
            referencia="signup_bonus_google",
        )
        db.add(credit)
        await db.flush()

        log.info("google_auth_new_user", usuario_id=str(user.id), email=email)
    else:
        # Existing user — link Google account if not already linked
        if user.google_id is None:
            user.google_id = google_id
        if photo_url:
            user.photo_url = photo_url
        await db.flush()
        log.info("google_auth_existing_user", usuario_id=str(user.id), email=email)

    if not user.activo:
        raise UnauthorizedError("Account is disabled")

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

    result = await db.execute(select(TokenBlacklist).where(TokenBlacklist.jti == jti))
    if result.scalar_one_or_none() is not None:
        raise UnauthorizedError("Token has been revoked")

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
    response = UserResponse.model_validate(user)
    response.photo_url = await _resolve_photo_url(response.photo_url)
    # Balance is derived from the credit ledger (source of truth), not the cache,
    # so it is always correct even if the denormalized cache ever drifts.
    from app.services import credito_service

    response.creditos_disponibles = await credito_service.obtener_saldo(db, user_id)
    return response


async def update_user(
    db: AsyncSession, user_id: uuid.UUID, data: UpdateUserRequest
) -> UserResponse:
    """Update user profile fields. If cedula is set for the first time, imports SECOP contracts."""
    result = await db.execute(select(Usuario).where(Usuario.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("Usuario", str(user_id))

    cedula_anterior = user.cedula

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)
    await db.flush()

    if not cedula_anterior and user.cedula:
        try:
            from app.services.secop_service import importar_contratos_secop
            result_secop = await importar_contratos_secop(
                db=db,
                documento_proveedor=user.cedula,
                usuario_id=user.id,
                confirmar=True,
            )
            log.info(
                "update_user_secop_import",
                usuario_id=str(user.id),
                cedula=user.cedula,
                importados=result_secop.importados,
            )
        except Exception:  # noqa: BLE001
            log.warning("update_user_secop_import_failed", cedula=user.cedula)

    response = UserResponse.model_validate(user)
    response.photo_url = await _resolve_photo_url(response.photo_url)
    return response


async def upload_profile_photo(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: bytes,
    content_type: str,
    storage: StoragePort,
) -> UserResponse:
    """Upload a profile photo to S3 and update photo_url with the S3 key."""
    ext = "jpg" if content_type == "image/jpeg" else content_type.split("/")[-1]
    key = f"avatars/{user_id}.{ext}"

    await storage.upload(key, data, content_type)

    result = await db.execute(select(Usuario).where(Usuario.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("Usuario", str(user_id))

    user.photo_url = key
    await db.flush()

    response = UserResponse.model_validate(user)
    response.photo_url = await storage.presigned_url(key, expires_in=604800)
    return response


async def logout(db: AsyncSession, token: str) -> None:
    """Blacklist the current access token so it can't be reused."""
    try:
        payload = decode_token(token)
    except JWTError:
        raise UnauthorizedError("Invalid token") from None

    jti = payload.get("jti", "")
    if not jti:
        raise UnauthorizedError("Invalid token: missing jti")

    result = await db.execute(select(TokenBlacklist).where(TokenBlacklist.jti == jti))
    if result.scalar_one_or_none() is not None:
        return

    exp_str = payload.get("exp", "")
    expires_at = datetime.fromtimestamp(float(exp_str), tz=UTC) if exp_str else datetime.now(UTC)
    blacklist_entry = TokenBlacklist(jti=jti, expires_at=expires_at)
    db.add(blacklist_entry)
    await db.flush()
