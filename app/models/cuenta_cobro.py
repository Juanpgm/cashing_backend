"""CuentaCobro model."""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDMixin


class EstadoCuentaCobro(enum.StrEnum):
    BORRADOR = "borrador"
    ENVIADA = "enviada"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"
    PAGADA = "pagada"


class PosicionCuota(enum.StrEnum):
    """Where a cuota sits in its contrato's sequence (billing-resilience-templates,
    slice #3). Stored, never inferred at read time — replaces the previous
    `checklist_service._is_first_cuenta` positional heuristic."""

    PRIMERA = "primera"
    RECURRENTE = "recurrente"
    FINAL = "final"


class CuentaCobro(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "cuentas_cobro"
    __table_args__ = (UniqueConstraint("contrato_id", "mes", "anio", name="uq_contrato_mes_anio"),)

    contrato_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("contratos.id"), nullable=False, index=True)
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
    # How the document checklist is built for this cuenta. NULL = not defined yet
    # (the post-creation gate must be resolved first). Once chosen: 'estandar',
    # 'augment' (standard + inferred) or 'reemplazar' (inferred + EVIDENCIAS only).
    requisitos_modo: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Cuota position model (billing-resilience-templates, slice #3, migration 025).
    # `numero_cuota` is the 1-based ordinal among the contrato's cuotas, derived and
    # persisted at creation time (`cuenta_cobro_service.crear_cuenta_cobro`) — nullable
    # only as a type-level safety net (`int | None`); every cuota created through the
    # service (and every legacy row, via the migration's backfill) has a value.
    # `posicion` defaults to RECURRENTE; `crear_cuenta_cobro` promotes it to PRIMERA for
    # the contrato's first cuota. `final` is NEVER inferred — it is only ever set
    # explicitly via `informe_final`.
    numero_cuota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posicion: Mapped[PosicionCuota] = mapped_column(
        # values_callable → store the lowercase `.value`s (matching migration
        # 025's enum labels), NOT the uppercase member names. Postgres enum
        # labels must agree with what the migration created (see documento_fuente
        # CategoriaDocumento precedent). Without this, INSERT/read break on Neon.
        Enum(PosicionCuota, name="posicion_cuota", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=PosicionCuota.RECURRENTE,
    )
    informe_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Transaction date (radicacion-stepper, work unit B1, migration 028). Nullable,
    # additive, non-breaking — legacy rows load with NULL. Set at stepper step 2;
    # used as the bounding date for month-scoped pipelines (evidence discovery,
    # justification) when present, falling back to mes/anio when absent.
    fecha_transaccion: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    contrato: Mapped["Contrato"] = relationship(back_populates="cuentas_cobro")  # type: ignore[name-defined]  # noqa: F821
    actividades: Mapped[list["Actividad"]] = relationship(back_populates="cuenta_cobro", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
    borradores: Mapped[list["BorradorCuentaCobro"]] = relationship(back_populates="cuenta_cobro", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
