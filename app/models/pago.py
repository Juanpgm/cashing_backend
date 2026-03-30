"""Pago model — payment records."""

import enum
import uuid

from sqlalchemy import JSON, Enum, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class EstadoPago(enum.StrEnum):
    PENDIENTE = "pendiente"
    APROBADO = "aprobado"
    RECHAZADO = "rechazado"
    ERROR = "error"


class TipoPago(enum.StrEnum):
    CREDITOS = "creditos"
    SUSCRIPCION = "suscripcion"


class Pago(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "pagos"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=False, index=True
    )
    referencia_wompi: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    monto: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    estado: Mapped[EstadoPago] = mapped_column(
        Enum(EstadoPago, name="estado_pago"), nullable=False, default=EstadoPago.PENDIENTE
    )
    tipo: Mapped[TipoPago] = mapped_column(
        Enum(TipoPago, name="tipo_pago"), nullable=False
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
