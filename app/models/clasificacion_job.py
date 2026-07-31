"""ClasificacionEvidenciasJob — background classification job state, one row per
CuentaCobro (evidence-classification-jobs capability).

ONE active job per cuenta is enforced by a unique constraint on
`cuenta_cobro_id` rather than a history table: (re)triggering classification
upserts the SAME row in place (status reset to pending, total/procesadas/error
cleared) instead of accumulating one row per run. This is also how "retry"
works — no separate retry endpoint/state machine needed.

Additive-only: no columns changed on any existing table, safe under SQLite
`create_all` + mirrored here for Postgres via Alembic.
"""

import enum
import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class EstadoClasificacionJob(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ClasificacionEvidenciasJob(UUIDMixin, TimestampMixin, Base):
    """Aggregate progress for one cuenta's background classification run.

    Per-file/per-obligación detail is NOT duplicated here — it's derived at
    read time from the `evidencia_obligacion` link table (see
    `evidence_classification_service.obtener_estado_clasificacion`): an
    evidencia with zero link rows while this job is `failed` is the
    "failed_retryable" signal, reusing the existing "every evidence resolves
    to a suggestion or explicit non-match" invariant instead of tracking a
    second, redundant per-file status.
    """

    __tablename__ = "clasificacion_evidencias_job"

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cuentas_cobro.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=EstadoClasificacionJob.PENDING.value)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    procesadas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("cuenta_cobro_id", name="uq_clasificacion_job_cuenta"),)
