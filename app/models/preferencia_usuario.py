"""PreferenciaUsuario model — key-value user preferences store (Phase 7)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDMixin


class PreferenciaUsuario(UUIDMixin, Base):
    """User-scoped key-value store for preferences and configuration.

    The ``valor`` column uses JSON so it can hold strings, numbers, booleans,
    lists, and nested dicts.

    Examples::

        { clave: "idioma",               valor: "es" }
        { clave: "moneda",               valor: "COP" }
        { clave: "modo_agente_default",  valor: "CUENTA_COBRO" }
        { clave: "plantilla_preferida",  valor: "uuid-string" }
        { clave: "notificaciones_email", valor: true }
        { clave: "mcp_servers",          valor: ["gmail", "drive", "calendar"] }
    """

    __tablename__ = "preferencias_usuario"
    __table_args__ = (
        UniqueConstraint("usuario_id", "clave", name="uq_preferencias_usuario_clave"),
    )

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    clave: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Preference key, e.g. 'idioma', 'modo_agente_default'",
    )
    valor: Mapped[Any] = mapped_column(
        JSON,
        nullable=True,
        comment="JSON value — string, number, bool, list, or dict",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    usuario: Mapped["Usuario"] = relationship(back_populates="preferencias")  # type: ignore[name-defined]  # noqa: F821
