"""Background evidence classification job (evidence-classification-jobs
capability).

`subir_evidencias_cuenta` used to run `_clasificar_y_enlazar_lote` inline,
adding embed()/LLM latency to the upload response. This module moves that
call into a FastAPI `BackgroundTasks` job so the upload response returns fast
— the job itself reuses the SAME `_clasificar_y_enlazar_lote` (no
reimplementation), keyed by ONE job row per cuenta (see
`app.models.clasificacion_job` docstring for the upsert/retry rationale).

The background task receives the request's own `AsyncSession` (the standard
FastAPI pattern: dependencies-with-yield close AFTER background tasks run) —
no separate engine/session factory, so it works unchanged against whatever
DB the request is already using (Postgres in prod, aiosqlite in tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.actividad import Actividad
from app.models.clasificacion_job import ClasificacionEvidenciasJob, EstadoClasificacionJob
from app.models.cuenta_cobro import CuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion
from app.schemas.evidencia_clasificacion import (
    ClasificacionEstadoResponse,
    EvidenciaClasificacionOut,
    ObligacionSugeridaOut,
)

logger = structlog.get_logger("service.evidence_classification")


async def _evidencias_sin_clasificar(db: AsyncSession, cuenta_id: uuid.UUID) -> list[Evidencia]:
    """Evidencias uploaded for this cuenta with NO `evidencia_obligacion` row
    yet — idempotent input set for both the first run and any retry (a retry
    naturally re-processes whatever is still unresolved, including files that
    failed last run)."""
    linked_result = await db.execute(
        select(EvidenciaObligacion.evidencia_id)
        .join(Evidencia, Evidencia.id == EvidenciaObligacion.evidencia_id)
        .join(Actividad, Actividad.id == Evidencia.actividad_id)
        .where(Actividad.cuenta_cobro_id == cuenta_id)
    )
    linked_ids = {row[0] for row in linked_result.all()}

    result = await db.execute(
        select(Evidencia)
        .join(Actividad, Actividad.id == Evidencia.actividad_id)
        .where(Actividad.cuenta_cobro_id == cuenta_id)
        .order_by(Evidencia.created_at.asc())
    )
    return [ev for ev in result.scalars().all() if ev.id not in linked_ids]


async def _upsert_job(db: AsyncSession, cuenta_id: uuid.UUID, total: int) -> tuple[ClasificacionEvidenciasJob, bool]:
    """ONE row per cuenta (evidence-classification-jobs: one active job per
    cuenta) — (re)triggering always resets the same row rather than piling up
    history, which is also how "retry a failed job" works: no separate
    endpoint/state machine, just call this again.

    Returns `(job, should_enqueue)`. `should_enqueue` is False ONLY for a
    still-fresh `running` job — an idempotent no-op that prevents a duplicate
    concurrent run (RES-002). A `running` job with no progress for longer than
    `EVIDENCE_JOB_STALE_SECONDS` is treated as a crashed/orphaned run and reset
    like any other non-running state.
    """
    result = await db.execute(
        select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
    )
    job = result.scalar_one_or_none()

    if job is not None and job.status == EstadoClasificacionJob.RUNNING.value:
        updated_at = job.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
        if age_seconds < settings.EVIDENCE_JOB_STALE_SECONDS:
            return job, False  # fresh running job — idempotent no-op

    if job is None:
        job = ClasificacionEvidenciasJob(cuenta_cobro_id=cuenta_id)
        db.add(job)
    job.status = EstadoClasificacionJob.PENDING.value
    job.total = total
    job.procesadas = 0
    job.error = None
    await db.commit()
    await db.refresh(job)
    return job, True


async def encolar_clasificacion(
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    cuenta_id: uuid.UUID,
) -> ClasificacionEvidenciasJob:
    """Upsert the job row (pending) and schedule the actual run in the
    background — returns immediately (evidence-classification-jobs: Fast
    upload response / Background classification execution). No-op re-enqueue
    for an already-fresh `running` job (RES-002)."""
    pendientes = await _evidencias_sin_clasificar(db, cuenta_id)
    job, should_enqueue = await _upsert_job(db, cuenta_id, total=len(pendientes))
    if should_enqueue:
        background_tasks.add_task(_ejecutar_clasificacion, db, cuenta_id)
    return job


async def _ejecutar_clasificacion(db: AsyncSession, cuenta_id: uuid.UUID) -> None:
    """The actual background run. Fail-open at the job level: any unexpected
    exception marks the job `failed` with the error message, but every
    already-committed link (from evidencias processed before the failure)
    stays intact, and every Evidencia row persists regardless of the outcome
    (evidence-classification-jobs: Retryable failure state, Evidence
    persistence independent of classification outcome).
    """
    # ponytail: no lock — a concurrent trigger for the same cuenta races on the
    # same job row (last write wins). Acceptable for MVP single-worker
    # BackgroundTasks; add a per-cuenta lock if a real task queue replaces this.
    from app.services.evidencia_service import _clasificar_y_enlazar_lote

    job_result = await db.execute(
        select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
    )
    job = job_result.scalar_one_or_none()
    if job is None:
        return

    job.status = EstadoClasificacionJob.RUNNING.value
    await db.commit()

    try:
        evidencias = await _evidencias_sin_clasificar(db, cuenta_id)

        cuenta_result = await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))
        cuenta = cuenta_result.scalar_one_or_none()
        obligaciones: list[Obligacion] = []
        if cuenta is not None:
            ob_result = await db.execute(select(Obligacion).where(Obligacion.contrato_id == cuenta.contrato_id))
            obligaciones = list(ob_result.scalars().all())

        await _clasificar_y_enlazar_lote(db, obligaciones, evidencias, job=job)

        job.status = EstadoClasificacionJob.DONE.value
        job.procesadas = len(evidencias)
        await db.commit()
        logger.info(
            "clasificacion_job_done",
            cuenta_cobro_id=str(cuenta_id),
            total=job.total,
            procesadas=job.procesadas,
        )
    except Exception as exc:
        try:
            await db.rollback()
            # `rollback()` expires every attribute on `job` — refresh before
            # mutating it, otherwise SQLAlchemy needs a synchronous lazy-load to
            # read the old value for history tracking (MissingGreenlet outside an
            # awaited context).
            await db.refresh(job)
            job.status = EstadoClasificacionJob.FAILED.value
            job.error = str(exc)[:2000]
            await db.commit()
            logger.warning("clasificacion_job_failed", cuenta_id=str(cuenta_id), error=str(exc))
        except Exception as recovery_exc:
            # RES-001: the session/connection may already be dead by this point
            # (e.g. the original failure WAS a connection loss) — rollback()/
            # refresh()/commit() can themselves raise. Never let that escape
            # the background task (it would otherwise leave the job stuck
            # `running` forever with no failure ever recorded).
            logger.error(
                "clasificacion_job_recovery_failed",
                cuenta_cobro_id=str(cuenta_id),
                error=str(recovery_exc),
            )


async def obtener_estado_clasificacion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> ClasificacionEstadoResponse:
    """Per-file/per-obligación progress (evidence-classification-jobs: Per-
    file/per-obligación progress status endpoint).

    Per-file `estado` is DERIVED, not stored: an evidencia with >=1 link row
    is "clasificado"; with zero link rows while the job is "failed" it's
    "failed_retryable" (reuses the Phase 3 invariant that every evidence ends
    with a link OR an explicit non-match — a job-completed evidencia with no
    link at all means classification failed for it); otherwise "pendiente".
    """
    from app.services import cuenta_cobro_service

    await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    job_result = await db.execute(
        select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
    )
    job = job_result.scalar_one_or_none()
    job_status = job.status if job is not None else EstadoClasificacionJob.PENDING.value
    total = job.total if job is not None else 0
    procesadas = job.procesadas if job is not None else 0
    error = job.error if job is not None else None

    ev_result = await db.execute(
        select(Evidencia)
        .join(Actividad, Actividad.id == Evidencia.actividad_id)
        .where(Actividad.cuenta_cobro_id == cuenta_id)
        .order_by(Evidencia.created_at.asc())
    )
    evidencias = ev_result.scalars().all()

    links_result = await db.execute(
        select(EvidenciaObligacion)
        .join(Evidencia, Evidencia.id == EvidenciaObligacion.evidencia_id)
        .join(Actividad, Actividad.id == Evidencia.actividad_id)
        .where(Actividad.cuenta_cobro_id == cuenta_id)
    )
    links_por_evidencia: dict[uuid.UUID, list[EvidenciaObligacion]] = {}
    for link in links_result.scalars().all():
        links_por_evidencia.setdefault(link.evidencia_id, []).append(link)

    items: list[EvidenciaClasificacionOut] = []
    for ev in evidencias:
        ev_links = links_por_evidencia.get(ev.id, [])
        if ev_links:
            estado = "clasificado"
        elif job_status == EstadoClasificacionJob.FAILED.value:
            estado = "failed_retryable"
        else:
            estado = "pendiente"
        items.append(
            EvidenciaClasificacionOut(
                id=ev.id,
                nombre_archivo=ev.nombre_archivo,
                estado=estado,
                obligaciones=[
                    ObligacionSugeridaOut(
                        obligacion_id=link.obligacion_id,
                        confianza=link.confianza,
                        status=link.status,
                        reasoning=link.reasoning,
                    )
                    for link in ev_links
                ],
            )
        )

    return ClasificacionEstadoResponse(
        cuenta_cobro_id=cuenta_id,
        status=job_status,
        total=total,
        procesadas=procesadas,
        error=error,
        evidencias=items,
    )
