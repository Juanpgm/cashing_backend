"""EvidenciaObligacion model — many-to-many link between Evidencia and Obligacion,
carrying per-link confidence and reasoning (evidence-obligation-links capability).

Additive-only: `Evidencia.actividad_id` and `Obligacion` are untouched. This table
is the classification source of truth; `Evidencia.actividad_id` keeps pointing at
its (possibly stub) Actividad regardless of how many obligaciones a file backs.
"""

import enum
import uuid

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class ConfianzaEnlace(enum.StrEnum):
    """Qualitative confidence only — the matcher's raw score is never exposed
    directly (evidence-obligation-links: Qualitative confidence levels)."""

    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"


class EstadoEnlace(enum.StrEnum):
    """PROPOSED (media/baja, awaiting user confirmation) and CONFIRMED (alta,
    auto-linked) are the two "real" link states. REJECTED is a link the user
    explicitly removed. NO_APLICA represents "no obligación applies to this
    evidence" — stored with `obligacion_id=None`, distinct from simply having
    zero link rows for that evidencia."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NO_APLICA = "no_aplica"


class FuenteEnlace(enum.StrEnum):
    AI = "ai"
    USER = "user"


class EvidenciaObligacion(UUIDMixin, TimestampMixin, Base):
    """One row per (evidencia, obligación) relationship — NOT a foreign key column
    on Evidencia, so one evidence file can back N obligaciones and vice versa.

    Plain VARCHAR columns for confianza/status/source (not a native Postgres ENUM)
    — same trade-off as `plantillas_organismo.tipo_documento` (migration 027):
    avoids the SQLAlchemy Enum `values_callable` gotcha for a brand-new table.

    `obligacion_id` is nullable ONLY to represent the "no obligación applies"
    state (`status=NO_APLICA`) as a row scoped to the evidencia with no specific
    obligación — every other status always carries a real `obligacion_id`.
    """

    __tablename__ = "evidencia_obligacion"

    evidencia_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("evidencias.id"), nullable=False, index=True)
    obligacion_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("obligaciones.id"), nullable=True, index=True
    )
    # Nullable: a `NO_APLICA` row (no obligación applies) carries no qualitative
    # confidence — every other status always sets one of alta/media/baja.
    confianza: Mapped[str | None] = mapped_column(String(10), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=EstadoEnlace.PROPOSED.value)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default=FuenteEnlace.AI.value)

    __table_args__ = (UniqueConstraint("evidencia_id", "obligacion_id", name="uq_evidencia_obligacion"),)
