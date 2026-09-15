"""PaqueteJob — background package-generation job state, one row per
CuentaCobro (radicacion-sin-friccion, Phase 2 slice 2.7).

Mirrors `app.models.clasificacion_job.ClasificacionEvidenciasJob` exactly:
ONE active job per cuenta is enforced by a unique constraint on
`cuenta_cobro_id` rather than a history table — (re)triggering upserts the
SAME row in place instead of accumulating one row per run. This is also how
"retry a failed run" works — no separate retry endpoint/state machine needed.

Unlike `ClasificacionEvidenciasJob` (which tracks incremental per-file
`total`/`procesadas` progress), package generation is an atomic pipeline
(checklist -> coherence -> packager, see `radicacion_prep_service.
preparar_radicacion`) with no meaningful partial-progress counter — so this
row instead persists the FULL result shape (`RadicacionPrepResultado`'s
fields) once the run reaches `done`, so both a poller (`GET /paquete/job`)
and the synchronous caller that lost the lock (see `paquete_job_service`'s
module docstring) can read the same outcome without re-running the pipeline.

Additive-only: no columns changed on any existing table, safe under SQLite
`create_all` + mirrored here for Postgres via Alembic (`042_paquete_job`).
"""

import enum
import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class EstadoPaqueteJob(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PaqueteJob(UUIDMixin, TimestampMixin, Base):
    """Aggregate state for one cuenta's package-generation run.

    `storage_key`/`filename`/`size_bytes`/`listo_para_radicar`/`pendientes`/
    `advertencias_coherencia`/`es_borrador` mirror `radicacion_prep_service.
    RadicacionPrepResultado`'s fields exactly (all `None` until the run
    reaches `done`). `advertencias_coherencia` stores `[dataclasses.asdict(f)
    for f in resultado.advertencias_coherencia]` as JSON — `Finding.severity`
    is a `StrEnum` (a `str` subclass), so it round-trips through `json.dumps`
    with no extra serialization step.

    `error`/`error_code` mirror a failed run's `DomainError.detail`/`.code`
    (e.g. `CHECKLIST_INCOMPLETE`, `COHERENCE_CHECK_FAILED`, `PACKAGE_
    PENDIENTE`, `SECRET_DETECTED_IN_PACKAGE`) so a poller or a lock-losing
    synchronous caller can reconstruct/report the exact same failure the
    winning run hit, not a generic message.
    """

    __tablename__ = "paquete_job"

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cuentas_cobro.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=EstadoPaqueteJob.PENDING.value)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    listo_para_radicar: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pendientes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    es_borrador: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    advertencias_coherencia: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (UniqueConstraint("cuenta_cobro_id", name="uq_paquete_job_cuenta"),)
