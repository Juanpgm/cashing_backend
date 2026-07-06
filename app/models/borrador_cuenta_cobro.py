"""BorradorCuentaCobro model — versioned drafts of a cuenta de cobro."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDMixin


class BorradorCuentaCobro(UUIDMixin, Base):
    __tablename__ = "borradores_cuenta_cobro"

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("cuentas_cobro.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="1",
        comment="Monotonically increasing draft version number",
    )
    contenido: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        comment="Full rendered content of this draft version",
    )
    diff: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="JSON diff against previous version (null for v1)",
    )
    feedback_usuario: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="User feedback used to generate next version"
    )
    aprobado: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="True when user approves this version for PDF generation",
    )
    aprobado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("cuenta_cobro_id", "version", name="uq_borradores_cuenta_cobro_version"),
    )

    # Relationships
    cuenta_cobro: Mapped["CuentaCobro"] = relationship(back_populates="borradores")  # type: ignore[name-defined]  # noqa: F821
