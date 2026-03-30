"""Actividad model."""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class Actividad(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "actividades"

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cuentas_cobro.id"), nullable=False, index=True
    )
    obligacion_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("obligaciones.id"), nullable=True
    )
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    justificacion: Mapped[str | None] = mapped_column(Text, nullable=True)
    fecha_realizacion: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    cuenta_cobro: Mapped["CuentaCobro"] = relationship(back_populates="actividades")  # type: ignore[name-defined]  # noqa: F821
    obligacion: Mapped["Obligacion | None"] = relationship(back_populates="actividades")  # type: ignore[name-defined]  # noqa: F821
    evidencias: Mapped[list["Evidencia"]] = relationship(back_populates="actividad", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
