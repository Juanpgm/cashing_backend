"""Suscripcion model."""

import enum
import uuid
from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class PlanSuscripcion(enum.StrEnum):
    FREE = "free"
    BASICO = "basico"
    PRO = "pro"


class Suscripcion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "suscripciones"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=False, index=True
    )
    plan: Mapped[PlanSuscripcion] = mapped_column(
        Enum(PlanSuscripcion, name="plan_suscripcion"), nullable=False, default=PlanSuscripcion.FREE
    )
    creditos_mensuales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fecha_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_fin: Mapped[date | None] = mapped_column(Date, nullable=True)
    activa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    usuario: Mapped["Usuario"] = relationship(back_populates="suscripciones")  # type: ignore[name-defined]  # noqa: F821
