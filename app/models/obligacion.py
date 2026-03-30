"""Obligacion model."""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TipoObligacion(enum.StrEnum):
    GENERAL = "general"
    ESPECIFICA = "especifica"


class Obligacion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "obligaciones"

    contrato_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contratos.id"), nullable=False, index=True
    )
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    tipo: Mapped[TipoObligacion] = mapped_column(
        Enum(TipoObligacion, name="tipo_obligacion"), nullable=False
    )
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Relationships
    contrato: Mapped["Contrato"] = relationship(back_populates="obligaciones")  # type: ignore[name-defined]  # noqa: F821
    actividades: Mapped[list["Actividad"]] = relationship(back_populates="obligacion", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
