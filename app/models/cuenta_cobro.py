"""CuentaCobro model."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDMixin


class EstadoCuentaCobro(enum.StrEnum):
    BORRADOR = "borrador"
    ENVIADA = "enviada"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"
    PAGADA = "pagada"


class CuentaCobro(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "cuentas_cobro"
    __table_args__ = (UniqueConstraint("contrato_id", "mes", "anio", name="uq_contrato_mes_anio"),)

    contrato_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contratos.id"), nullable=False, index=True
    )
    mes: Mapped[int] = mapped_column(Integer, nullable=False)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    estado: Mapped[EstadoCuentaCobro] = mapped_column(
        Enum(EstadoCuentaCobro, name="estado_cuenta_cobro"),
        nullable=False,
        default=EstadoCuentaCobro.BORRADOR,
    )
    valor: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    pdf_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fecha_envio: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    contrato: Mapped["Contrato"] = relationship(back_populates="cuentas_cobro")  # type: ignore[name-defined]  # noqa: F821
    actividades: Mapped[list["Actividad"]] = relationship(back_populates="cuenta_cobro", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
