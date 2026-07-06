"""Google Workspace service — OAuth, Gmail, Drive business logic."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drive.drive_adapter import DriveAdapter, build_contract_drive_path
from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.google_token import GoogleToken
from app.schemas.google_workspace import (
    DriveUploadResponse,
    EmailSearchResponse,
    EmailSendResponse,
    GoogleConnectURLResponse,
    GoogleIntegrationStatus,
)

logger = structlog.get_logger("services.google_workspace")

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_OAUTH_STATE_TYPE = "oauth_state"


def _fernet() -> Fernet:
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


# ── OAuth signed-state helpers ────────────────────────────────────────────────


def _encode_oauth_state(usuario_id: uuid.UUID, code_verifier: str) -> str:
    """Sign a short-lived JWT carrying usuario_id and PKCE code_verifier for the OAuth round-trip."""
    now = datetime.now(UTC)
    claims = {
        "sub": str(usuario_id),
        "type": _OAUTH_STATE_TYPE,
        "cv": code_verifier,
        "iat": now,
        "exp": now + timedelta(seconds=settings.GOOGLE_OAUTH_STATE_TTL_SECONDS),
    }
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_oauth_state(state: str) -> tuple[uuid.UUID, str]:
    """Decode and validate a signed OAuth state token.

    Returns (usuario_id, code_verifier). Raises ValidationError on invalid/expired/tampered tokens.
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
    except (KeyError, ValueError) as exc:
        raise ValidationError("State OAuth sin datos válidos") from exc
    return usuario_id, code_verifier


# ── OAuth ────────────────────────────────────────────────────────────────────


def get_authorization_url(usuario_id: uuid.UUID) -> GoogleConnectURLResponse:
    """Build the Google OAuth2 authorization URL for the user to visit.

    Uses PKCE and embeds the code_verifier + usuario_id in a signed state JWT so
    the callback can recover both without a DB lookup or a separate session store.
    """
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise ExternalServiceError("Google OAuth", "CLIENT_ID y CLIENT_SECRET no configurados")

    code_verifier = secrets.token_urlsafe(96)  # 128 chars — satisfies RFC 7636 length requirement
    state = _encode_oauth_state(usuario_id, code_verifier)

    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=settings.GOOGLE_OAUTH_SCOPES,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
    )
    flow.code_verifier = code_verifier  # type: ignore[assignment]
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return GoogleConnectURLResponse(authorization_url=auth_url, state=state)


async def handle_oauth_callback(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    code: str,
    code_verifier: str,
) -> GoogleIntegrationStatus:
    """Exchange authorization code for tokens and persist encrypted in GoogleToken."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise ExternalServiceError("Google OAuth", "No configurado")

    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=settings.GOOGLE_OAUTH_SCOPES,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
    )
    flow.code_verifier = code_verifier  # type: ignore[assignment]

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.error("google_oauth_token_exchange_failed", error=str(exc))
        raise ExternalServiceError("Google OAuth", f"Error intercambiando código: {exc}") from exc

    creds: Credentials = flow.credentials
    await store_credentials(
        db,
        usuario_id,
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        scopes=creds.scopes or settings.GOOGLE_OAUTH_SCOPES,
    )
    logger.info("google_oauth_connected", user_id=str(usuario_id))

    return await get_integration_status(db, usuario_id)


async def store_credentials(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    *,
    access_token: str,
    refresh_token: str,
    scopes: list[str] | str,
    expires_in: int = 3600,
) -> GoogleToken:
    """Encrypt and upsert a user's Google OAuth tokens.

    Shared by the web OAuth callback and the local loopback demo script so both
    persist tokens identically (Fernet-encrypted, one row per user).
    """
    f = _fernet()
    scope_str = " ".join(scopes) if isinstance(scopes, (list, tuple)) else str(scopes)
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

    result = await db.execute(select(GoogleToken).where(GoogleToken.usuario_id == usuario_id))
    record = result.scalar_one_or_none()

    if record:
        record.access_token_encrypted = f.encrypt(access_token.encode()).decode()
        record.refresh_token_encrypted = f.encrypt(refresh_token.encode()).decode()
        record.scopes = scope_str
        record.expires_at = expires_at
    else:
        record = GoogleToken(
            usuario_id=usuario_id,
            access_token_encrypted=f.encrypt(access_token.encode()).decode(),
            refresh_token_encrypted=f.encrypt(refresh_token.encode()).decode(),
            scopes=scope_str,
            expires_at=expires_at,
        )
        db.add(record)

    await db.commit()
    return record


async def revoke_integration(db: AsyncSession, usuario_id: uuid.UUID) -> None:
    """Delete GoogleToken — disconnects Google account."""
    result = await db.execute(select(GoogleToken).where(GoogleToken.usuario_id == usuario_id))
    record = result.scalar_one_or_none()
    if not record:
        raise NotFoundError("Integración de Google")
    await db.delete(record)
    await db.commit()
    logger.info("google_integration_revoked", user_id=str(usuario_id))


async def get_integration_status(db: AsyncSession, usuario_id: uuid.UUID) -> GoogleIntegrationStatus:
    """Return connection status and enabled scopes for the user."""
    result = await db.execute(select(GoogleToken).where(GoogleToken.usuario_id == usuario_id))
    record = result.scalar_one_or_none()

    if not record:
        return GoogleIntegrationStatus(connected=False)

    scopes = record.scopes.split()
    gmail_enabled = any(_GMAIL_SCOPE in s for s in scopes)
    drive_enabled = any(_DRIVE_SCOPE in s for s in scopes)

    # Decrypt email from access token (via Google tokeninfo endpoint) only if needed
    return GoogleIntegrationStatus(
        connected=True,
        scopes=scopes,
        expires_at=record.expires_at,
        gmail_enabled=gmail_enabled,
        drive_enabled=drive_enabled,
    )


# ── Gmail ────────────────────────────────────────────────────────────────────


async def search_emails(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    query: str,
    max_results: int = 20,
) -> EmailSearchResponse:
    from app.agent.prompts.evidence_filter import score_non_personal_email
    from app.schemas.google_workspace import EmailAttachmentResponse, EmailMessageResponse

    adapter = GmailAdapter(db)
    # Fetch a larger pool so filtering still yields max_results personal emails.
    fetch_limit = min(max_results * 4, 100)
    messages = await adapter.search_messages(usuario_id, query, fetch_limit)

    responses = []
    for m in messages:
        if len(responses) >= max_results:
            break
        score, _ = score_non_personal_email(
            sender=m.sender,
            subject=m.subject,
            labels=list(m.labels or []),
            headers=dict(m.headers or {}),
        )
        if score >= 3:
            continue
        responses.append(
            EmailMessageResponse(
                id=m.id,
                thread_id=m.thread_id,
                subject=m.subject,
                sender=m.sender,
                recipients=m.recipients,
                date=m.date,
                snippet=m.snippet,
                body_plain=m.body_plain,
                body_html=m.body_html,
                attachments=[
                    EmailAttachmentResponse(
                        attachment_id=a.attachment_id,
                        filename=a.filename,
                        mime_type=a.mime_type,
                        size_bytes=a.size_bytes,
                    )
                    for a in m.attachments
                ],
                labels=m.labels,
            )
        )
    return EmailSearchResponse(messages=responses, total=len(responses), query_used=query)


async def send_invoice_email(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    to: list[str],
    subject: str,
    body_html: str,
    pdf_bytes: bytes | None = None,
    pdf_filename: str | None = None,
) -> EmailSendResponse:
    adapter = GmailAdapter(db)
    attachments = None
    if pdf_bytes and pdf_filename:
        attachments = [(pdf_filename, pdf_bytes, "application/pdf")]

    message_id = await adapter.send_message(
        usuario_id=usuario_id,
        to=to,
        subject=subject,
        body_html=body_html,
        attachments=attachments,
    )
    return EmailSendResponse(message_id=message_id, sent_to=to, subject=subject)


# ── Drive ────────────────────────────────────────────────────────────────────


async def upload_pdf_to_drive(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    pdf_bytes: bytes,
    filename: str,
    entidad: str,
    numero_contrato: str,
    anio: int,
    mes: int,
    make_shareable: bool = False,
) -> DriveUploadResponse:
    adapter = DriveAdapter(db)
    path = build_contract_drive_path(entidad, numero_contrato, anio, mes)
    folder_id = await adapter.get_or_create_folder(usuario_id, path)

    drive_file = await adapter.upload_file(
        usuario_id=usuario_id,
        name=filename,
        content=pdf_bytes,
        mime_type="application/pdf",
        folder_id=folder_id,
    )

    share_link = None
    if make_shareable:
        share_link = await adapter.make_shareable(usuario_id, drive_file.id)

    return DriveUploadResponse(
        file_id=drive_file.id,
        name=drive_file.name,
        folder_path=path,
        web_view_link=drive_file.web_view_link,
        share_link=share_link,
    )
