"""Google Workspace service — OAuth, Gmail, Drive business logic.

Token/state/status persistence now delegates to `integration_service` (the
generalized, provider-agnostic credential store) — this module keeps only the
Google-specific `Flow` construction and Gmail/Drive operations. See
openspec/changes/microsoft-365-integration/design.md D2.
"""

from __future__ import annotations

import secrets
import uuid

import structlog
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drive.drive_adapter import DriveAdapter, build_contract_drive_path
from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.config import settings
from app.core.exceptions import ExternalServiceError
from app.models.integracion import Integracion, IntegrationProvider
from app.schemas.google_workspace import (
    DriveUploadResponse,
    EmailSearchResponse,
    EmailSendResponse,
    GoogleConnectURLResponse,
    GoogleIntegrationStatus,
)
from app.services import integration_service

logger = structlog.get_logger("services.google_workspace")


def verify_oauth_state(state: str) -> tuple[uuid.UUID, str]:
    """Decode and validate a signed OAuth state token.

    Returns (usuario_id, code_verifier). Raises ValidationError on invalid/expired/tampered tokens.
    Thin backward-compatible wrapper — the real (now provider-aware) logic lives
    in `integration_service.verify_oauth_state`.
    """
    usuario_id, code_verifier, _provider = integration_service.verify_oauth_state(state)
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
    state = integration_service.encode_oauth_state(usuario_id, code_verifier, IntegrationProvider.GOOGLE)

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
    """Exchange authorization code for tokens and persist encrypted in `integraciones`."""
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
) -> Integracion:
    """Encrypt and upsert a user's Google OAuth tokens.

    Shared by the web OAuth callback and the local loopback demo script so both
    persist tokens identically. Thin wrapper — delegates to
    `integration_service.store_credentials` with `provider=google`.
    """
    return await integration_service.store_credentials(
        db,
        usuario_id,
        IntegrationProvider.GOOGLE,
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=scopes,
        expires_in=expires_in,
    )


async def revoke_integration(db: AsyncSession, usuario_id: uuid.UUID) -> None:
    """Disconnect the user's Google account (deletes its `integraciones` row)."""
    await integration_service.revoke_integration(db, usuario_id, IntegrationProvider.GOOGLE)


async def get_integration_status(db: AsyncSession, usuario_id: uuid.UUID) -> GoogleIntegrationStatus:
    """Return connection status and enabled scopes for the user's Google connection."""
    status = await integration_service.get_integration_status(db, usuario_id, IntegrationProvider.GOOGLE)
    return GoogleIntegrationStatus(
        connected=status.connected,
        email=status.email,
        scopes=status.scopes,
        expires_at=status.expires_at,
        gmail_enabled=status.mail_enabled,
        drive_enabled=status.drive_enabled,
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
