"""ImapAccount model — credentials for a generic (non-OAuth) IMAP mailbox.

Separate from `Integracion` (Google/Microsoft OAuth credential store, see
app/models/integracion.py) on purpose: IMAP has no access/refresh token,
scopes, or expiry — it's host/port/username/password. Reusing `Integracion`'s
OAuth-shaped columns for that data would be a schema abuse (and its
`ck_integraciones_provider` CHECK constraint only allows 'google'/'microsoft'
today), so this is its own small table instead.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class ImapAccount(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "imap_accounts"
    __table_args__ = (UniqueConstraint("usuario_id", "email", name="uq_imap_accounts_usuario_email"),)

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
