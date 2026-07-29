"""Integration service — shared, provider-agnostic credential/state helpers.

Owns the security-sensitive core shared by every provider's OAuth flow:
Fernet token encryption, signed-JWT state (now provider-aware), and the
generalized `integraciones` credential store (create/read/update/delete).
Per-provider OAuth `Flow` construction and API calls stay in each provider's
own service module (`google_workspace_service.py`, `microsoft_graph_service.py`),
which delegate here instead of re-implementing crypto/state/persistence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.fernet import Fernet
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.integracion import Integracion, IntegrationProvider
from app.schemas.integracion import IntegrationStatus

logger = structlog.get_logger("services.integration")

_OAUTH_STATE_TYPE = "oauth_state"

# Scope substrings used to derive IntegrationStatus's per-source enabled flags,
# computed from granted-scope membership at read time (tasks.md Reconciliation #4).
_SCOPE_MARKERS: dict[IntegrationProvider, dict[str, str]] = {
    IntegrationProvider.GOOGLE: {
        "mail_enabled": "https://www.googleapis.com/auth/gmail",
        "drive_enabled": "https://www.googleapis.com/auth/drive",
        "calendar_enabled": "https://www.googleapis.com/auth/calendar",
    },
    IntegrationProvider.MICROSOFT: {
        "mail_enabled": "Mail.Read",
        "drive_enabled": "Files.Read",
        "calendar_enabled": "Calendars.Read",
    },
}


def _fernet() -> Fernet:
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


def _derive_enabled_flags(provider: IntegrationProvider, scopes: list[str]) -> dict[str, bool]:
    markers = _SCOPE_MARKERS.get(provider, {})
    return {flag: any(marker in s for s in scopes) for flag, marker in markers.items()}


# ── OAuth signed-state helpers (provider-aware) ───────────────────────────────


def encode_oauth_state(usuario_id: uuid.UUID, code_verifier: str, provider: IntegrationProvider) -> str:
    """Sign a short-lived JWT carrying usuario_id, PKCE code_verifier, and provider."""
    now = datetime.now(UTC)
    claims = {
        "sub": str(usuario_id),
        "type": _OAUTH_STATE_TYPE,
        "cv": code_verifier,
        "provider": provider.value,
        "iat": now,
        "exp": now + timedelta(seconds=settings.GOOGLE_OAUTH_STATE_TTL_SECONDS),
    }
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_oauth_state(state: str) -> tuple[uuid.UUID, str, IntegrationProvider]:
    """Decode and validate a signed OAuth state token.

    Returns (usuario_id, code_verifier, provider). Raises ValidationError on
    invalid/expired/tampered tokens. States encoded before the `provider` claim
    existed default to `google` (backward compatible with in-flight redirects).
    """
    try:
        payload = jwt.decode(state, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise ValidationError("State OAuth inválido o expirado") from exc
    if payload.get("type") != _OAUTH_STATE_TYPE:
        raise ValidationError("State OAuth de tipo incorrecto")
    try:
        usuario_id = uuid.UUID(payload["sub"])
        code_verifier = payload["cv"]
        provider = IntegrationProvider(payload.get("provider", IntegrationProvider.GOOGLE.value))
    except (KeyError, ValueError) as exc:
        raise ValidationError("State OAuth sin datos válidos") from exc
    return usuario_id, code_verifier, provider


# ── Credential store ───────────────────────────────────────────────────────


async def store_credentials(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    provider: IntegrationProvider,
    *,
    access_token: str,
    refresh_token: str,
    scopes: list[str] | str,
    expires_in: int = 3600,
    email: str | None = None,
) -> Integracion:
    """Encrypt and upsert a user's OAuth tokens, keyed on (usuario_id, provider, email)."""
    f = _fernet()
    scope_str = " ".join(scopes) if isinstance(scopes, (list, tuple)) else str(scopes)
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    email_value = email or ""

    result = await db.execute(
        select(Integracion).where(
            Integracion.usuario_id == usuario_id,
            Integracion.provider == provider,
            Integracion.email == email_value,
        )
    )
    record = result.scalar_one_or_none()

    if record:
        record.access_token_encrypted = f.encrypt(access_token.encode()).decode()
        record.refresh_token_encrypted = f.encrypt(refresh_token.encode()).decode()
        record.scopes = scope_str
        record.expires_at = expires_at
    else:
        record = Integracion(
            usuario_id=usuario_id,
            provider=provider,
            access_token_encrypted=f.encrypt(access_token.encode()).decode(),
            refresh_token_encrypted=f.encrypt(refresh_token.encode()).decode(),
            scopes=scope_str,
            expires_at=expires_at,
            email=email_value,
        )
        db.add(record)

    await db.commit()
    await db.refresh(record)
    return record


async def revoke_integration(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    provider: IntegrationProvider,
    *,
    email: str | None = None,
) -> None:
    """Delete a user's stored credentials for one provider — disconnects the account.

    A user may hold multiple accounts per (usuario_id, provider), distinguished by
    `email`. Pass `email` to target one specific account; otherwise the most
    recently updated row is resolved deterministically (`.scalars().first()`
    instead of `.scalar_one_or_none()`, which raises MultipleResultsFound once a
    second account exists — see openspec/changes/microsoft-365-integration).
    """
    query = select(Integracion).where(Integracion.usuario_id == usuario_id, Integracion.provider == provider)
    if email is not None:
        query = query.where(Integracion.email == email)
    query = query.order_by(Integracion.updated_at.desc())
    result = await db.execute(query)
    record = result.scalars().first()
    if not record:
        raise NotFoundError(f"Integración de {provider.value}")
    await db.delete(record)
    await db.commit()
    logger.info("integration_revoked", user_id=str(usuario_id), provider=provider.value)


def _to_status(record: Integracion) -> IntegrationStatus:
    scopes = record.scopes.split()
    flags = _derive_enabled_flags(record.provider, scopes)
    return IntegrationStatus(
        provider=record.provider,
        connected=True,
        email=record.email or None,
        scopes=scopes,
        expires_at=record.expires_at,
        mail_enabled=flags.get("mail_enabled", False),
        drive_enabled=flags.get("drive_enabled", False),
        calendar_enabled=flags.get("calendar_enabled", False),
    )


async def get_integration_status(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    provider: IntegrationProvider,
    *,
    email: str | None = None,
) -> IntegrationStatus:
    """Return connection status and enabled scopes for one (usuario, provider).

    Pass `email` to target one specific account among several stored for this
    (usuario_id, provider); otherwise the most recently updated row is returned
    (see `revoke_integration` docstring for why `.scalars().first()` is required
    here instead of `.scalar_one_or_none()`).
    """
    query = select(Integracion).where(Integracion.usuario_id == usuario_id, Integracion.provider == provider)
    if email is not None:
        query = query.where(Integracion.email == email)
    query = query.order_by(Integracion.updated_at.desc())
    result = await db.execute(query)
    record = result.scalars().first()
    if not record:
        return IntegrationStatus(provider=provider, connected=False)
    return _to_status(record)


async def list_integration_statuses(db: AsyncSession, usuario_id: uuid.UUID) -> list[IntegrationStatus]:
    """Return one IntegrationStatus per provider the user is connected to."""
    result = await db.execute(select(Integracion).where(Integracion.usuario_id == usuario_id))
    return [_to_status(record) for record in result.scalars().all()]


async def has_any_connected_provider(db: AsyncSession, usuario_id: uuid.UUID) -> bool:
    """True if the user has at least one connected provider (any account)."""
    result = await db.execute(select(Integracion.id).where(Integracion.usuario_id == usuario_id).limit(1))
    return result.scalar_one_or_none() is not None
