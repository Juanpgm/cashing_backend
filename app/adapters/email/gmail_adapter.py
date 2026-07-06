"""Gmail adapter — Google Gmail implementation of EmailPort."""

from __future__ import annotations

import asyncio
import base64
import email as email_lib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TypeVar

import structlog
from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as GoogleHttpError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.email.port import EmailAttachment, EmailMessage
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.google_token import GoogleToken

logger = structlog.get_logger("adapters.gmail")

_T = TypeVar("_T")

# Gmail enforces a per-user concurrency cap. Fanning out one get_message() per
# search hit at once trips HTTP 429 "Too many concurrent requests for user".
# Bound the fan-out and retry rate-limited calls with exponential backoff.
_GMAIL_MAX_CONCURRENCY = 5
_GMAIL_MAX_RETRIES = 4
_GMAIL_RETRY_BASE_DELAY = 0.5  # seconds; doubles each attempt (0.5, 1, 2, ...)


def _is_rate_limit_error(exc: GoogleHttpError) -> bool:
    """True if a Gmail HttpError is a rate-limit / concurrency error (HTTP 429)."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is not None and str(status) == "429":
        return True
    text = str(exc).lower()
    return "ratelimitexceeded" in text or "too many concurrent requests" in text


class GmailAdapter:
    """Google Gmail implementation of EmailPort.

    Uses encrypted OAuth tokens stored in GoogleToken model.
    All Google API calls run via run_in_executor to avoid blocking the event loop.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())

    # ── Credential Management ────────────────────────────────────────────────

    async def get_credentials(self, usuario_id: uuid.UUID) -> Credentials:
        """Load, decrypt, and refresh Google OAuth credentials for a user.

        When GOOGLE_USE_ADC=true and no DB token exists, falls back to Application
        Default Credentials — useful for local dev without a full OAuth client setup.
        """
        result = await self._db.execute(
            select(GoogleToken).where(GoogleToken.usuario_id == usuario_id)
        )
        record = result.scalar_one_or_none()
        if not record:
            if settings.GOOGLE_USE_ADC:
                import google.auth  # lazy import — only needed for local dev ADC path

                adc_creds, _ = google.auth.default(scopes=settings.GOOGLE_OAUTH_SCOPES)
                logger.info("google_adc_fallback", user_id=str(usuario_id))
                return adc_creds  # type: ignore[return-value]
            raise NotFoundError("Cuenta de Google no conectada. Ve a /api/v1/integraciones/google/connect")

        access_token = self._fernet.decrypt(record.access_token_encrypted.encode()).decode()
        refresh_token = self._fernet.decrypt(record.refresh_token_encrypted.encode()).decode()

        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=settings.GOOGLE_OAUTH_SCOPES,
        )

        # Refresh if expired
        expires_at = record.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, lambda: creds.refresh(Request()))
            except (RefreshError, TransportError) as exc:
                raise ExternalServiceError(
                    "Google OAuth",
                    f"Token vencido — reconectá tu cuenta de Google en /integraciones: {exc}",
                ) from exc
            record.access_token_encrypted = self._fernet.encrypt(creds.token.encode()).decode()
            record.expires_at = datetime.now(UTC) + timedelta(seconds=3600)
            await self._db.commit()
            logger.info("google_token_refreshed", user_id=str(usuario_id))

        return creds

    def _build_service(self, creds: Credentials):  # type: ignore[no-untyped-def]
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    async def _execute_with_retry(self, fn: Callable[[], _T]) -> _T:
        """Run a blocking Google API call in the executor, retrying on 429 with backoff.

        Non-rate-limit HttpErrors propagate immediately. Rate-limit errors are retried
        up to _GMAIL_MAX_RETRIES times with exponentially growing delays.
        """
        loop = asyncio.get_running_loop()
        last_exc: GoogleHttpError | None = None
        for attempt in range(_GMAIL_MAX_RETRIES):
            try:
                return await loop.run_in_executor(None, fn)
            except GoogleHttpError as exc:
                if not _is_rate_limit_error(exc) or attempt == _GMAIL_MAX_RETRIES - 1:
                    raise
                last_exc = exc
                delay = _GMAIL_RETRY_BASE_DELAY * (2**attempt)
                logger.warning("gmail_rate_limited_retry", attempt=attempt + 1, delay=delay)
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]  # unreachable — loop either returns or raises

    # ── Search ───────────────────────────────────────────────────────────────

    async def search_messages(
        self,
        usuario_id: uuid.UUID,
        query: str,
        max_results: int = 20,
    ) -> list[EmailMessage]:
        # Fetch (and refresh) credentials ONCE to avoid an N+1 burst of DB lookups.
        # A FRESH service is built per request, though: googleapiclient's underlying
        # httplib2.Http is NOT thread-safe, so sharing one across the concurrent
        # executor threads corrupts the TLS socket (SSL RECORD_LAYER_FAILURE).
        # Each build() is local (static discovery), so this is cheap.
        creds = await self.get_credentials(usuario_id)
        service = self._build_service(creds)

        def _search() -> dict:  # type: ignore[type-arg]
            return (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )

        try:
            result = await self._execute_with_retry(_search)
        except GoogleHttpError as exc:
            raise ExternalServiceError("Gmail", f"Error buscando correos: {exc}") from exc
        raw_messages = result.get("messages", [])

        if not raw_messages:
            return []

        # Bound the fan-out so Gmail's per-user concurrency limit is never exceeded.
        semaphore = asyncio.Semaphore(_GMAIL_MAX_CONCURRENCY)

        async def _bounded_fetch(message_id: str) -> EmailMessage:
            async with semaphore:
                return await self._fetch_message(creds, message_id)

        tasks = [_bounded_fetch(msg["id"]) for msg in raw_messages]
        return list(await asyncio.gather(*tasks))

    async def get_message(self, usuario_id: uuid.UUID, message_id: str) -> EmailMessage:
        creds = await self.get_credentials(usuario_id)
        return await self._fetch_message(creds, message_id)

    async def _fetch_message(self, creds: Credentials, message_id: str) -> EmailMessage:
        """Fetch and parse a single message.

        Builds its OWN Gmail service (and thus its own httplib2.Http socket) so it is
        safe to call concurrently from multiple executor threads. Reuses the shared,
        already-refreshed credentials — only the cheap, thread-local service is rebuilt.
        """
        service = self._build_service(creds)

        def _get() -> dict:  # type: ignore[type-arg]
            return (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )

        try:
            raw = await self._execute_with_retry(_get)
        except GoogleHttpError as exc:
            raise ExternalServiceError("Gmail", f"Error obteniendo mensaje {message_id}: {exc}") from exc
        return self._parse_message(raw)

    async def get_attachment(
        self,
        usuario_id: uuid.UUID,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        creds = await self.get_credentials(usuario_id)
        service = self._build_service(creds)

        def _get_att() -> dict:  # type: ignore[type-arg]
            return (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )

        result = await self._execute_with_retry(_get_att)
        data = result.get("data", "")
        return base64.urlsafe_b64decode(data + "==")

    # ── Send ─────────────────────────────────────────────────────────────────

    async def send_message(
        self,
        usuario_id: uuid.UUID,
        to: list[str],
        subject: str,
        body_html: str,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> str:
        """Send email via Gmail. Returns Gmail message_id."""
        if not to:
            raise ValidationError("Se requiere al menos un destinatario")

        creds = await self.get_credentials(usuario_id)
        service = self._build_service(creds)

        msg = MIMEMultipart("mixed")
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        if attachments:
            for filename, content, mime_type in attachments:
                main_type, sub_type = mime_type.split("/", 1)
                part = MIMEBase(main_type, sub_type)
                part.set_payload(content)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                msg.attach(part)

        raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        def _send() -> dict:  # type: ignore[type-arg]
            return (
                service.users()
                .messages()
                .send(userId="me", body={"raw": raw_bytes})
                .execute()
            )

        try:
            result = await self._execute_with_retry(_send)
        except GoogleHttpError as exc:
            raise ExternalServiceError("Gmail", f"Error enviando correo: {exc}") from exc
        logger.info("email_sent", user_id=str(usuario_id), to=to, subject=subject)
        return result["id"]

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _parse_message(self, raw: dict) -> EmailMessage:  # type: ignore[type-arg]
        raw_headers = {h["name"]: h["value"] for h in raw["payload"].get("headers", [])}
        date_str = raw_headers.get("Date", "")
        try:
            date = email_lib.utils.parsedate_to_datetime(date_str)
        except Exception:
            date = datetime.now(UTC)

        body_plain, body_html = self._extract_body(raw["payload"])
        attachments = self._extract_attachments(raw["payload"])

        return EmailMessage(
            id=raw["id"],
            thread_id=raw.get("threadId", ""),
            subject=raw_headers.get("Subject", "(sin asunto)"),
            sender=raw_headers.get("From", ""),
            recipients=raw_headers.get("To", "").split(", "),
            date=date,
            body_plain=body_plain,
            body_html=body_html,
            snippet=raw.get("snippet", ""),
            attachments=attachments,
            labels=raw.get("labelIds", []),
            headers=dict(raw_headers),
        )

    def _extract_body(self, payload: dict) -> tuple[str, str | None]:  # type: ignore[type-arg]
        plain, html = "", None

        def _walk(part: dict) -> None:  # type: ignore[type-arg]
            nonlocal plain, html
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if data:
                decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                if mime == "text/plain" and not plain:
                    plain = decoded
                elif mime == "text/html" and not html:
                    html = decoded
            for sub in part.get("parts", []):
                _walk(sub)

        _walk(payload)
        return plain, html

    def _extract_attachments(self, payload: dict) -> list[EmailAttachment]:  # type: ignore[type-arg]
        attachments: list[EmailAttachment] = []

        def _walk(part: dict) -> None:  # type: ignore[type-arg]
            filename = part.get("filename", "")
            attachment_id = part.get("body", {}).get("attachmentId", "")
            if filename and attachment_id:
                attachments.append(
                    EmailAttachment(
                        attachment_id=attachment_id,
                        filename=filename,
                        mime_type=part.get("mimeType", "application/octet-stream"),
                        size_bytes=part.get("body", {}).get("size", 0),
                    )
                )
            for sub in part.get("parts", []):
                _walk(sub)

        _walk(payload)
        return attachments
