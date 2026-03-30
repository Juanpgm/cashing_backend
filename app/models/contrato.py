"""Contrato model."""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDMixin


class Contrato(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "contratos"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=False, index=True
    )
    numero_contrato: Mapped[str] = mapped_column(String(100), nullable=False)
    objeto: Mapped[str] = mapped_column(Text, nullable=False)
    valor_total: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    valor_mensual: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    fecha_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_fin: Mapped[date] = mapped_column(Date, nullable=False)
    supervisor_nombre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entidad: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dependencia: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fuente_documento_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documentos_fuente.id"), nullable=True
    )

    # Relationships
    usuario: Mapped["Usuario"] = relationship(back_populates="contratos")  # type: ignore[name-defined]  # noqa: F821
    obligaciones: Mapped[list["Obligacion"]] = relationship(back_populates="contrato", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
    cuentas_cobro: Mapped[list["CuentaCobro"]] = relationship(back_populates="contrato", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
