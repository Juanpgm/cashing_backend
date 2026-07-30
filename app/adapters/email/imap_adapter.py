"""IMAP email adapter — generic (non-OAuth) implementation of EmailPort.

For mailboxes NOT hosted on Gmail or Microsoft 365 (custom domain, on-prem
Exchange, cPanel, etc.) — plain IMAP over SSL via stdlib `imaplib`, credentials
are host/port/username/password rather than an OAuth token (see
app/models/imap_account.py for why this doesn't reuse `Integracion`).

`imaplib` is synchronous/blocking — every call runs via `run_in_executor`,
same pattern as gmail_adapter.py's googleapiclient wrapping.

Read-only: IMAP has no send capability (that's SMTP, out of scope here) and
no per-attachment fetch-by-id API the way Gmail/Graph do, so `send_message`/
`get_attachment` raise `NotImplementedError` rather than pretending to work.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import uuid
from datetime import UTC, datetime
from email.header import decode_header
from typing import Any

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.email.port import EmailMessage
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.imap_account import ImapAccount

logger = structlog.get_logger("adapters.imap")


def _open_connection(host: str, port: int, use_ssl: bool) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    return "".join(
        part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, enc in decode_header(value)
    )


def _extract_raw_bytes(msg_data: list[Any]) -> bytes | None:
    """imaplib's FETCH response items are `tuple[bytes, bytes] | bytes` — only the
    tuple form carries the actual RFC822 content; other items (e.g. the closing
    `)`) are None. Real content is always in the tuple's second element."""
    if not msg_data:
        return None
    item = msg_data[0]
    return item[1] if isinstance(item, tuple) else None


def _parse_message(msg_id: str, raw_bytes: bytes) -> EmailMessage:
    msg = email_lib.message_from_bytes(raw_bytes)
    subject = _decode_header_value(msg.get("Subject", ""))
    sender = _decode_header_value(msg.get("From", ""))
    recipients = [r.strip() for r in msg.get("To", "").split(",") if r.strip()]
    try:
        date = email_lib.utils.parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        date = datetime.now(UTC)

    body_plain, body_html = "", None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/plain" and not body_plain:
                body_plain = text
            elif ctype == "text/html" and not body_html:
                body_html = text
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            body_plain = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    return EmailMessage(
        id=msg_id,
        thread_id="",  # IMAP has no thread concept comparable to Gmail's threadId
        subject=subject or "(sin asunto)",
        sender=sender,
        recipients=recipients,
        date=date,
        body_plain=body_plain,
        body_html=body_html,
        snippet=body_plain[:200],
        attachments=[],
        labels=[],
        headers=dict(msg.items()),
    )


class ImapAdapter:
    """Generic IMAP implementation of EmailPort (read-only)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())

    # ── Credential management ────────────────────────────────────────────────

    async def connect_account(
        self,
        usuario_id: uuid.UUID,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        email_address: str | None = None,
    ) -> None:
        """Validate credentials against the real server, then store them encrypted.

        Validation-before-store means a typo'd password never silently sits in the
        DB looking "connected" — the same real login the adapter will later perform.
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._login_and_logout(host, port, username, password, use_ssl))
        except imaplib.IMAP4.error as exc:
            raise ValidationError(f"No se pudo conectar al servidor IMAP: {exc}") from exc

        target_email = email_address or username
        result = await self._db.execute(
            select(ImapAccount).where(ImapAccount.usuario_id == usuario_id, ImapAccount.email == target_email)
        )
        record = result.scalar_one_or_none()
        password_enc = self._fernet.encrypt(password.encode()).decode()
        if record:
            record.host = host
            record.port = port
            record.username = username
            record.password_encrypted = password_enc
            record.use_ssl = use_ssl
        else:
            record = ImapAccount(
                usuario_id=usuario_id,
                email=target_email,
                host=host,
                port=port,
                username=username,
                password_encrypted=password_enc,
                use_ssl=use_ssl,
            )
            self._db.add(record)
        await self._db.commit()
        logger.info("imap_account_connected", user_id=str(usuario_id), host=host)

    def _login_and_logout(self, host: str, port: int, username: str, password: str, use_ssl: bool) -> None:
        conn = _open_connection(host, port, use_ssl)
        try:
            conn.login(username, password)
        finally:
            conn.logout()

    async def _get_account(self, usuario_id: uuid.UUID) -> ImapAccount:
        result = await self._db.execute(
            select(ImapAccount).where(ImapAccount.usuario_id == usuario_id).order_by(ImapAccount.updated_at.desc())
        )
        record = result.scalars().first()
        if not record:
            raise NotFoundError("Cuenta IMAP no conectada")
        return record

    def _decrypt_password(self, record: ImapAccount) -> str:
        try:
            return self._fernet.decrypt(record.password_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise ExternalServiceError("IMAP", "No se pudo desencriptar la credencial almacenada") from exc

    # ── EmailPort ─────────────────────────────────────────────────────────────

    async def search_messages(self, usuario_id: uuid.UUID, query: str, max_results: int = 20) -> list[EmailMessage]:
        record = await self._get_account(usuario_id)
        password = self._decrypt_password(record)
        loop = asyncio.get_running_loop()
        try:
            messages = await loop.run_in_executor(None, lambda: self._search_sync(record, password, query, max_results))
        except imaplib.IMAP4.error as exc:
            raise ExternalServiceError("IMAP", f"Error buscando correos: {exc}") from exc
        logger.info("imap_search", user_id=str(usuario_id), count=len(messages))
        return messages

    def _search_sync(self, record: ImapAccount, password: str, query: str, max_results: int) -> list[EmailMessage]:
        conn = _open_connection(record.host, record.port, record.use_ssl)
        try:
            conn.login(record.username, password)
            conn.select("INBOX", readonly=True)
            criteria = f'(TEXT "{query}")' if query else "ALL"
            status, data = conn.search(None, criteria)
            if status != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()[-max_results:]  # IMAP ids ascend by arrival — tail = most recent N
            messages: list[EmailMessage] = []
            for msg_id in reversed(ids):
                fetch_status, msg_data = conn.fetch(msg_id, "(RFC822)")
                raw_bytes = _extract_raw_bytes(msg_data) if fetch_status == "OK" else None
                if raw_bytes is None:
                    continue
                messages.append(_parse_message(msg_id.decode(), raw_bytes))
            return messages
        finally:
            conn.logout()

    async def get_message(self, usuario_id: uuid.UUID, message_id: str) -> EmailMessage:
        record = await self._get_account(usuario_id)
        password = self._decrypt_password(record)
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: self._fetch_one_sync(record, password, message_id))
        except imaplib.IMAP4.error as exc:
            raise ExternalServiceError("IMAP", f"Error obteniendo mensaje {message_id}: {exc}") from exc

    def _fetch_one_sync(self, record: ImapAccount, password: str, message_id: str) -> EmailMessage:
        conn = _open_connection(record.host, record.port, record.use_ssl)
        try:
            conn.login(record.username, password)
            conn.select("INBOX", readonly=True)
            status, msg_data = conn.fetch(message_id, "(RFC822)")
            raw_bytes = _extract_raw_bytes(msg_data) if status == "OK" else None
            if raw_bytes is None:
                raise NotFoundError(f"Mensaje IMAP {message_id}")
            return _parse_message(message_id, raw_bytes)
        finally:
            conn.logout()

    async def get_attachment(self, usuario_id: uuid.UUID, message_id: str, attachment_id: str) -> bytes:
        raise NotImplementedError("IMAP attachment fetch by id is out of scope — not needed by evidence discovery")

    async def send_message(
        self,
        usuario_id: uuid.UUID,
        to: list[str],
        subject: str,
        body_html: str,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> str:
        raise NotImplementedError("IMAP is read-only — sending mail requires SMTP, out of scope")
