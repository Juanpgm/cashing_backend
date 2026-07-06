"""RequisitoCuenta — custom (inferred or manual) requirement scoped to ONE cuenta de cobro.

Mirrors the global catalog ``RequisitoDocumento`` but lives per cuenta: it is the
definition of a requirement that the contracting entity demands for a specific
process and that is not part of the standard catalog (or that overrides it).

The per-cuenta STATE of each requirement is still tracked in
``DocumentoCuentaCobro`` (which references either a catalog ``requisito_codigo``
or a ``requisito_cuenta_id`` pointing here). This keeps the same
definition ↔ state separation the project already uses for the standard catalog.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class RequisitoCuenta(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "requisitos_cuenta"
    __table_args__ = (UniqueConstraint("cuenta_cobro_id", "codigo", name="uq_reqcuenta_cuenta_codigo"),)

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cuentas_cobro.id", ondelete="CASCADE"), nullable=False, index=True
    )
    codigo: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="UPPER_SNAKE slug, unique within the cuenta. NOT a FK to the global catalog.",
    )
    etiqueta: Mapped[str] = mapped_column(String(200), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    obligatorio: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    solo_primera_cuenta: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tipo_documento_fuente: Mapped[str | None] = mapped_column(String(50), nullable=True)
    keywords_deteccion: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    mapea_a_estandar: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="If set, this item equals a standard requisito_codigo (e.g. 'RUT'); "
        "avoids duplicating it when merging with the standard catalog.",
    )
    origen: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="inferido",
        comment="'inferido' (from a document/text via LLM) or 'manual' (added by the user).",
    )
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
