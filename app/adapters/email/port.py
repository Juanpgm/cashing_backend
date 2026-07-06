"""Email port (interface) — abstract contract for email operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class EmailAttachment:
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    subject: str
    sender: str
    recipients: list[str]
    date: datetime
    body_plain: str
    snippet: str
    body_html: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


class EmailPort(Protocol):
    """Abstract email interface — implemented by Gmail or any other provider.

    The agent and services only depend on this protocol, never on Google SDKs.
    """

    async def search_messages(
        self,
        usuario_id: uuid.UUID,
        query: str,
        max_results: int = 20,
    ) -> list[EmailMessage]:
        """Search messages using provider-native query syntax."""
        ...

    async def get_message(
        self,
        usuario_id: uuid.UUID,
        message_id: str,
    ) -> EmailMessage:
        """Fetch full message content including body and attachments metadata."""
        ...

    async def get_attachment(
        self,
        usuario_id: uuid.UUID,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        """Download attachment bytes by ID."""
        ...

    async def send_message(
        self,
        usuario_id: uuid.UUID,
        to: list[str],
        subject: str,
        body_html: str,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> str:
        """Send email. attachments: list of (filename, content, mime_type). Returns message_id."""
        ...
