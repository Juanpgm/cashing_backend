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

VERIFIED, not assumed (radicacion-sin-friccion slice 4.2, investigation point
4): unlike slice 3.10a's `agent_chat_service.stream_chat_with_tools` bug (a raw
`asyncio.Task` inside an SSE generator, sharing the request's session across a
REAL client disconnect that tears it down mid-flight), `encolar_clasificacion`
uses FastAPI's NATIVE `background_tasks: BackgroundTasks` (`Depends`-injected).
Per FastAPI's documented order of events, a `yield`-based dependency's
post-`yield` code (here, `get_db`'s `session.commit()` + connection close)
runs AFTER every scheduled `BackgroundTasks` entry has finished — so sharing
`db` here is safe by construction, not by luck. Proven empirically (not just
cited) by `tests/test_evidence_classification_background_session.py`, which
calls the real endpoint through the app's actual `get_db` injection and
asserts the job reaches `done` with real links, not a closed-session error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError
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


class UpsertJobRetryExhaustedError(Exception):
    """`_upsert_job`'s 2-attempt insert-collision retry loop did not resolve
    (radicacion-sin-friccion slice 4.2, round-3 review): a dedicated, narrow
    type instead of a bare `RuntimeError`. `encolar_clasificacion` catches
    ONLY this specific type to remap it to a clean `NotFoundError` — a bare
    `RuntimeError` catch would also silently swallow any UNRELATED
    `RuntimeError` raised anywhere else in `_upsert_job`'s call graph (e.g.
    asyncio's "Event loop is closed"/"attached to a different loop", or any
    other bug), misreporting it as "CuentaCobro not found" and hiding a real
    bug behind a wrong 404."""


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
    history. This is how "retry a failed/stuck job" works: no separate
    endpoint/state machine — but note the freshness window below: a job stuck
    in `pending` (its background task never actually ran, e.g. a process
    restart mid-flight) is NOT immediately retriggerable by just calling this
    again; it stays a no-op until `EVIDENCE_JOB_STALE_SECONDS` (120s) elapses,
    because the freshness check covers `pending OR running` (see below).

    Returns `(job, should_enqueue)`. `should_enqueue` is False ONLY for a
    still-fresh `pending` OR `running` job — an idempotent no-op that prevents
    a duplicate concurrent/rapid-fire run (RES-002; widened from `running`-only
    in slice 4.2 — see below). A `pending`/`running` job with no progress for
    longer than `EVIDENCE_JOB_STALE_SECONDS` is treated as a crashed/orphaned
    run and reset like any other non-running state.

    Concurrency (radicacion-sin-friccion slice 4.2, RES-002 double-enqueue
    fix): two concurrent calls for the SAME cuenta used to both read a
    non-running state and both flip it to PENDING, double-scheduling
    `_ejecutar_clasificacion`. The freshness check was widened from
    `running`-only to `pending OR running`: under the lock below, the SECOND
    of two near-simultaneous callers now observes the FIRST caller's
    just-committed `PENDING` row (not yet `running` — its background task
    hasn't started executing yet) and must ALSO treat that as an in-flight,
    no-op case — otherwise it would still double-enqueue even with the lock in
    place, since `PENDING` alone never used to short-circuit.

    KNOWN GAP — scope of the "suppressed enqueue is still safe" guarantee
    (radicacion-sin-friccion slice 4.2 concurrency audit): the claim that a
    suppressed (no-op) call is harmless because `_ejecutar_clasificacion`
    re-derives its work set fresh from `_evidencias_sin_clasificar` at RUN
    time holds ONLY for the `pending` window — VERIFIED: an evidencia uploaded
    while the job is still `pending` (its background task hasn't sampled
    `_evidencias_sin_clasificar` yet) is picked up once that run actually
    executes. It does NOT hold for the `running` window — PRE-EXISTING gap,
    not introduced by this slice (the old code had the identical
    `running`-only short-circuit with the identical gap): if an evidencia is
    uploaded AFTER `_ejecutar_clasificacion` has already sampled its work set
    for the in-flight run, a second `encolar_clasificacion` call during that
    same `running` window is suppressed as a no-op, and that evidencia is
    NOT picked up by the in-flight run — it silently sits unclassified with
    no error anywhere (`obtener_estado_clasificacion` then reports it
    `pendiente` while the job reports `done`, and nothing resolves it
    automatically). Fixing this for real needs either re-sampling the work set
    inside `_ejecutar_clasificacion` right before it finishes, or a
    changed-since-start check — a larger design change out of this slice's
    scope. Flagged here rather than silently fixed or ignored: the next
    explicit `encolar_clasificacion` call AFTER the job leaves `running`/`done`
    will pick the evidencia up.

    Two distinct races are guarded here:
    - An EXISTING row is re-selected with `SELECT ... FOR UPDATE` before the
      running/stale decision, so a second concurrent caller serializes on this
      row and observes the first caller's already-committed state. No-op on
      SQLite/aiosqlite (tests); a real row lock on Postgres — same idiom as
      `secop_service.py`'s reimport guard and
      `cuenta_cobro_service._leer_estado_bajo_lock`.
    - A FIRST-EVER trigger for a cuenta has no row to lock (phantom insert) —
      `uq_clasificacion_job_cuenta` is the backstop there: the loser's INSERT
      raises `IntegrityError`, caught below, rolled back, and retried ONCE — by
      then the winner's row is committed, so the retry's SELECT finds it and
      takes the locked-update branch. Same rollback-and-retry idiom as
      `requisito_inference_service.py`'s `_intentar_ingerir`.
    """
    for _intento in range(2):
        result = await db.execute(
            select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
        )
        job = result.scalar_one_or_none()

        if job is not None:
            lock_result = await db.execute(
                select(ClasificacionEvidenciasJob)
                .where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
                .with_for_update()
            )
            job = lock_result.scalar_one()

            if job.status in (EstadoClasificacionJob.PENDING.value, EstadoClasificacionJob.RUNNING.value):
                updated_at = job.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
                if age_seconds < settings.EVIDENCE_JOB_STALE_SECONDS:
                    return job, False  # fresh pending/running job — idempotent no-op

            job.status = EstadoClasificacionJob.PENDING.value
            job.total = total
            job.procesadas = 0
            job.error = None
            await db.commit()
            await db.refresh(job)
            return job, True

        job = ClasificacionEvidenciasJob(
            cuenta_cobro_id=cuenta_id,
            status=EstadoClasificacionJob.PENDING.value,
            total=total,
            procesadas=0,
        )
        db.add(job)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue  # a concurrent first-trigger won — retry sees its row and locks it
        await db.refresh(job)
        return job, True

    raise UpsertJobRetryExhaustedError(f"_upsert_job: persistent insert collision for cuenta_cobro_id={cuenta_id}")


async def encolar_clasificacion(
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    cuenta_id: uuid.UUID,
) -> ClasificacionEvidenciasJob:
    """Upsert the job row (pending) and schedule the actual run in the
    background — returns immediately (evidence-classification-jobs: Fast
    upload response / Background classification execution). No-op re-enqueue
    for an already-fresh `running` job (RES-002).

    `_upsert_job`'s `UpsertJobRetryExhaustedError` (its 2-attempt retry loop
    exhausted without resolving the insert collision) is remapped to a clean
    `NotFoundError` here rather than left to surface as a raw 500 on the
    direct classify-trigger endpoint (`POST /{cuenta_id}/evidencias/clasificar`).
    The retry loop always resolves a genuine unique-constraint race within its
    first retry (the loser's second SELECT finds the winner's now-committed
    row); the only realistic way to still exhaust both attempts is the cuenta
    itself being hard-deleted concurrently, which turns the second INSERT
    attempt's collision into an FK violation instead — i.e. by the time this
    is reached, the cuenta plausibly no longer exists, which is exactly what
    `NotFoundError` communicates to the caller.

    Deliberately catches ONLY `UpsertJobRetryExhaustedError`, not a bare
    `RuntimeError` (radicacion-sin-friccion slice 4.2, round-3 review): a
    broad `except RuntimeError` would also silently remap an unrelated
    `RuntimeError` from anywhere else in `_upsert_job`'s call graph into a
    misleading 404, hiding a real bug. An unrelated `RuntimeError` now
    propagates uncaught instead.
    """
    pendientes = await _evidencias_sin_clasificar(db, cuenta_id)
    try:
        job, should_enqueue = await _upsert_job(db, cuenta_id, total=len(pendientes))
    except UpsertJobRetryExhaustedError as exc:
        raise NotFoundError("CuentaCobro", str(cuenta_id)) from exc
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
