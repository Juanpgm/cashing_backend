"""AdicionContrato model — contract Adición/prórroga/otrosí event log.

Records each contract amendment as a discrete, queryable event (billing-resilience-
templates, slice #4, migration 026), replacing the previous single scalar
`Contrato.valor_adicion` field as the authoritative source for cuota position/final
detection and the coherence validator's R6 rule. `Contrato.valor_adicion` is kept for
back-compat (design D4) — it is never written by this model going forward.
"""

import enum
import uuid
from datetime import date

from sqlalchemy import Date, Enum, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TipoAdicion(enum.StrEnum):
    ADICION = "adicion"
    PRORROGA = "prorroga"
    OTROSI = "otrosi"


class AdicionContrato(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "adiciones_contrato"

    contrato_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("contratos.id"), nullable=False, index=True)
    tipo: Mapped[TipoAdicion] = mapped_column(
        # values_callable → lowercase `.value` labels matching migration 026's
        # enum type (same Postgres-enum-label gotcha as PosicionCuota).
        Enum(TipoAdicion, name="tipo_adicion", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    numero: Mapped[int] = mapped_column(Integer, nullable=False)
    rpc_nuevo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cdp_nuevo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Money never crosses this boundary as float (CLAUDE.md anti-pattern) — nullable
    # because the SECOP-side source (secop_service.obtener_adiciones_contrato) may not
    # be able to parse a peso amount from free text; None means "unknown value", never 0.
    valor_adicion: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    nueva_fecha_fin: Mapped[date | None] = mapped_column(Date, nullable=True)
    descripcion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fecha_evento: Mapped[date] = mapped_column(Date, nullable=False)

    # Relationships
    contrato: Mapped["Contrato"] = relationship(back_populates="adiciones")  # type: ignore[name-defined]  # noqa: F821
