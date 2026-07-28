"""Integracion model — generalized, multi-provider OAuth credential store.

Replaces the single-provider `google_tokens` table (kept read-only — see
alembic/versions/024_integraciones_table.py). One row per connected account;
a user may hold both a Google and a Microsoft connection (and, per provider,
multiple accounts distinguished by `email`).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class IntegrationProvider(enum.StrEnum):
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class Integracion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "integraciones"
    __table_args__ = (
        UniqueConstraint("usuario_id", "provider", "email", name="uq_integraciones_usuario_provider_email"),
        Index("ix_integraciones_usuario_provider", "usuario_id", "provider"),
    )

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # native_enum=False → portable VARCHAR + CHECK (works on aiosqlite; avoids a
    # Postgres native-enum-alter migration when a third provider is added later).
    provider: Mapped[IntegrationProvider] = mapped_column(
        Enum(IntegrationProvider, name="integracion_provider", native_enum=False, length=20),
        nullable=False,
    )
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(String(1000), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NOT NULL default '' (not nullable): Postgres treats NULLs as distinct, which
    # would let unknown-email rows silently bypass the unique constraint. Empty
    # string collapses unknown-email connections to one row per (usuario, provider).
    email: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")
