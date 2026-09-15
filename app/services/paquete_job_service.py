"""Package generation as a poll-based background job, per-cuenta lock
(radicacion-sin-friccion, Phase 2 slice 2.7 — mirrors `evidence_classification_
service`'s `ClasificacionEvidenciasJob` pattern; read that module's docstring
for the underlying lock/upsert/background-session idioms this one reuses
verbatim).

`preparar_radicacion` (checklist -> coherence -> packager, 2 LLM calls) has
been fully synchronous with NO per-cuenta lock: two concurrent `POST
/paquete/regenerar` calls for the same cuenta both ran the whole expensive
pipeline in parallel — wasted LLM cost/work (S3's atomic PutObject already
prevented storage corruption, but not the duplicate work itself).

DESIGN DECISION — Option B (additive), not Option A (breaking) — read before
changing either the sync or the async entry point:

`POST /{cuenta_id}/paquete/regenerar` returns `PreparaRadicacionResponse`
SYNCHRONOUSLY today, and has exactly ONE existing caller in this backend
(`cashing-frontend/lib/paquete-api.ts`'s `useRegenerarPaquete`, confirmed via
grep — out of scope this round, separate follow-up slice in that repo) plus
one in-repo agent tool wrapper (`app/tools/catalog/radicacion.py`'s
`preparar_radicacion` tool, which calls `radicacion_prep_service.
preparar_radicacion` DIRECTLY, not through this module — see KNOWN GAP below).
Changing its response shape to 202+job in place (mirroring `ClasificacionJob`'s
own new-endpoint convention) would BREAK that frontend caller until its own
follow-up slice lands — this codebase's own demonstrated convention this
session (`ToolContext.background_tasks` defaulting `None`, `Modal`'s
`fullScreenBelowMd` defaulting `false`, `ClasificacionJob` itself introducing
`/evidencias/clasificar` as a NEW endpoint alongside the pre-existing ones)
strongly favors additive-not-breaking. So:

- The EXISTING `POST /paquete/regenerar` keeps its exact response shape
  (`PreparaRadicacionResponse`, 200 OK) for the normal (non-concurrent) case —
  zero frontend impact from this slice alone.
- TWO NEW, genuinely additive endpoints are added: `POST
  /paquete/regenerar-async` (202 + `PaqueteJobResponse`, mirrors `POST
  /evidencias/clasificar`'s convention exactly) and `GET /paquete/job` (poll,
  mirrors `GET /evidencias/clasificacion`).
- The per-cuenta LOCK still applies to BOTH the existing sync endpoint and the
  new async one, sharing the SAME `PaqueteJob` row as the mutex (see
  `_upsert_job` below) — a concurrent sync-vs-sync, sync-vs-async, or
  async-vs-async pair for the same cuenta can never run the pipeline twice.
  The one caller that loses the race:
  - **async loser**: a true no-op, exactly like `ClasificacionJob`'s own
    `should_enqueue=False` contract — the client polls `GET /paquete/job`
    separately (`encolar_generacion_paquete`).
  - **sync loser**: this is new territory `ClasificacionJob` never had to
    solve (its trigger endpoint was ALREADY async/202, so a "loser" caller
    never needed a synchronous result). A sync loser here does NOT silently
    re-run the pipeline (defeats the lock) and does NOT block-and-wait inside
    the request for the winner to finish (a real wait-loop adds latency/
    complexity for what should be a rare interleaving in production — this
    endpoint is triggered by user clicks, not high-frequency polling). It
    fails FAST with `PaqueteGenerationInProgressError` (409,
    `PAQUETE_GENERACION_EN_CURSO`) instead — a small, explicit, deliberate
    behavior change scoped to ONLY the genuine-concurrent-overlap case
    (`generar_paquete_bajo_lock`). Every non-overlapping call — i.e. every
    call the current frontend actually makes today — is completely
    unaffected: same 200, same body, same latency.

KNOWN GAP (flagged, not silently fixed — out of this slice's scope): the
in-repo agent tool `app/tools/catalog/radicacion.py`'s `preparar_radicacion`
tool calls `radicacion_prep_service.preparar_radicacion` directly, bypassing
this module's lock entirely. An agent-triggered regenerate and an API-
triggered regenerate for the same cuenta can therefore still race each other
undetected. Routing that tool through `generar_paquete_bajo_lock` too was
judged out of scope for this backend-only slice (a third call path, not
named in the task) and is called out here for a deliberate follow-up decision
rather than silently left undocumented.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime

import structlog
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import DomainError, NotFoundError, PaqueteGenerationInProgressError
from app.models.paquete_job import EstadoPaqueteJob, PaqueteJob
from app.schemas.paquete_job import PaqueteJobResponse
from app.services import paquete_service, radicacion_prep_service
from app.services.radicacion_prep_service import RadicacionPrepResultado

logger = structlog.get_logger("service.paquete_job")


class UpsertPaqueteJobRetryExhaustedError(Exception):
    """`_upsert_job`'s 2-attempt insert-collision retry loop did not resolve
    — same narrow-type rationale as `evidence_classification_service.
    UpsertJobRetryExhaustedError`'s docstring (a bare `RuntimeError` catch
    would also silently swallow an unrelated bug elsewhere in this call
    graph)."""


async def _upsert_job(db: AsyncSession, cuenta_id: uuid.UUID) -> tuple[PaqueteJob, bool]:
    """ONE row per cuenta — (re)triggering always resets the same row rather
    than piling up history (same idiom as `evidence_classification_service.
    _upsert_job`; read that function's docstring for the full concurrency
    rationale, reused verbatim here):

    - An EXISTING row is re-selected with `SELECT ... FOR UPDATE` before the
      running/stale decision — a second concurrent caller serializes on this
      row (no-op on SQLite/aiosqlite tests; a real row lock on Postgres).
    - A FIRST-EVER trigger has no row to lock (phantom insert) —
      `uq_paquete_job_cuenta` is the backstop: the loser's INSERT raises
      `IntegrityError`, caught, rolled back, retried ONCE.
    - A fresh (`age_seconds < settings.PAQUETE_JOB_STALE_SECONDS`) `pending`
      OR `running` job is an in-flight, idempotent no-op (`should_enqueue=
      False`) — a `pending`/`running` job older than that is treated as
      crashed/orphaned and reset like any other terminal state.

    Returns `(job, should_enqueue)`.
    """
    for _intento in range(2):
        result = await db.execute(select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_id))
        job = result.scalar_one_or_none()

        if job is not None:
            lock_result = await db.execute(
                select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_id).with_for_update()
            )
            job = lock_result.scalar_one()

            if job.status in (EstadoPaqueteJob.PENDING.value, EstadoPaqueteJob.RUNNING.value):
                updated_at = job.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
                if age_seconds < settings.PAQUETE_JOB_STALE_SECONDS:
                    return job, False  # fresh pending/running job — idempotent no-op

            job.status = EstadoPaqueteJob.PENDING.value
            job.error = None
            job.error_code = None
            # Force this explicitly rather than relying on `onupdate=func.now()`
            # alone: SQLAlchemy's dirty-checking (`History.from_scalar_attribute`)
            # compares old vs new with `==` and emits NO UPDATE at all for an
            # attribute reassigned to the value it already holds. A row that is
            # ALREADY `pending`/`running` with `error`/`error_code` already
            # `None` — precisely the stale-crashed-job case this branch exists
            # to handle — would otherwise leave `updated_at` unchanged, so its
            # staleness clock never resets and a second concurrent caller
            # re-checking the same (still-stale) `age_seconds` would also
            # conclude the row is stale and also "reset" it, letting two
            # callers both win the lock. Proven empirically before this fix.
            job.updated_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(job)
            return job, True

        job = PaqueteJob(cuenta_cobro_id=cuenta_id, status=EstadoPaqueteJob.PENDING.value)
        db.add(job)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue  # a concurrent first-trigger won — retry sees its row and locks it
        await db.refresh(job)
        return job, True

    raise UpsertPaqueteJobRetryExhaustedError(
        f"_upsert_job: persistent insert collision for cuenta_cobro_id={cuenta_id}"
    )


async def _marcar_fallo(db: AsyncSession, job: PaqueteJob, error: str, error_code: str | None) -> None:
    """RES-001-style recovery guard (same idiom as `evidence_classification_
    service._ejecutar_clasificacion`'s except block): the session/connection
    may already be dead by this point (e.g. the original failure WAS a
    connection loss) — never let a failure HERE escape and leave the job
    stuck `running` forever with no failure ever recorded."""
    try:
        await db.rollback()
        # `rollback()` expires every attribute on `job` — refresh before
        # mutating it (MissingGreenlet otherwise, see the classification
        # service's identical comment).
        await db.refresh(job)
        job.status = EstadoPaqueteJob.FAILED.value
        job.error = error[:2000]
        job.error_code = error_code
        await db.commit()
        logger.warning("paquete_job_failed", cuenta_cobro_id=str(job.cuenta_cobro_id), error=error)
    except Exception as recovery_exc:
        logger.error(
            "paquete_job_recovery_failed",
            cuenta_cobro_id=str(job.cuenta_cobro_id),
            error=str(recovery_exc),
        )


async def _ejecutar_generacion_paquete(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> RadicacionPrepResultado:
    """The actual pipeline run — shared by BOTH the synchronous caller
    (`generar_paquete_bajo_lock`, awaited inline) and the background caller
    (`_ejecutar_generacion_paquete_background`, scheduled via
    `BackgroundTasks`). ALWAYS updates the job row (`running` -> `done`/
    `failed`) before returning or raising; RE-RAISES the original exception
    on failure — it is the caller's responsibility to decide whether that
    propagates (sync path, preserves today's exact error contract) or gets
    swallowed (background path, fail-open at the job level — mirrors
    `_ejecutar_clasificacion`'s "any unexpected exception marks the job
    failed but never crashes the background task" contract).

    Storage-orphan guard: `radicacion_prep_service.preparar_radicacion`
    already only calls `storage.upload(...)` AFTER `informe_service.
    generar_zip_evidencias` has fully succeeded (the ZIP is built ENTIRELY in
    memory first) — so any failure inside the pipeline, wrapped here or not,
    already uploads nothing. This function adds no new storage write of its
    own; it only adds the job-row bookkeeping around the existing atomic
    upload.
    """
    job_result = await db.execute(select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_id))
    job = job_result.scalar_one_or_none()
    if job is None:
        # Defensive — `_upsert_job` always creates the row before this is
        # ever scheduled/awaited; a missing row here would mean the cuenta
        # (and its job) were hard-deleted between enqueue and run.
        raise NotFoundError("PaqueteJob", str(cuenta_id))

    job.status = EstadoPaqueteJob.RUNNING.value
    await db.commit()

    try:
        resultado = await radicacion_prep_service.preparar_radicacion(db, usuario_id, cuenta_id)

        await db.refresh(job)
        job.status = EstadoPaqueteJob.DONE.value
        job.storage_key = resultado.storage_key
        job.filename = resultado.filename
        job.size_bytes = resultado.size_bytes
        job.listo_para_radicar = resultado.listo_para_radicar
        job.pendientes = resultado.pendientes
        job.advertencias_coherencia = [asdict(f) for f in resultado.advertencias_coherencia]
        job.es_borrador = resultado.es_borrador
        job.error = None
        job.error_code = None
        await db.commit()
        logger.info("paquete_job_done", cuenta_cobro_id=str(cuenta_id), storage_key=resultado.storage_key)
        return resultado
    except DomainError as exc:
        await _marcar_fallo(db, job, str(exc.detail), exc.code)
        raise
    except Exception as exc:
        await _marcar_fallo(db, job, str(exc), None)
        raise


async def _ejecutar_generacion_paquete_background(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> None:
    """`BackgroundTasks` entry point — fail-open at the job level (see
    `_ejecutar_generacion_paquete`'s docstring): the job row already records
    the failure by the time this re-raise reaches here, so it's safe (and
    required — an uncaught exception must never escape a background task) to
    swallow it after logging."""
    try:
        await _ejecutar_generacion_paquete(db, usuario_id, cuenta_id)
    except Exception as exc:  # fail-open at the job level, see docstring
        logger.warning("paquete_job_background_failed", cuenta_cobro_id=str(cuenta_id), error=str(exc))


async def encolar_generacion_paquete(
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> PaqueteJob:
    """Upsert the job row (pending) and schedule the actual run in the
    background — returns immediately (`POST /paquete/regenerar-async`).
    No-op re-enqueue for an already-fresh `pending`/`running` job — the
    client polls `GET /paquete/job` for the in-flight (or just-finished)
    outcome, exactly like `evidence_classification_service.
    encolar_clasificacion`.
    """
    cuenta = await paquete_service._get_cuenta_con_contrato_vivo(db, usuario_id, cuenta_id)
    try:
        job, should_enqueue = await _upsert_job(db, cuenta_id)
    except UpsertPaqueteJobRetryExhaustedError as exc:
        raise NotFoundError("CuentaCobro", str(cuenta_id)) from exc
    if should_enqueue:
        background_tasks.add_task(_ejecutar_generacion_paquete_background, db, usuario_id, cuenta.id)
    return job


async def generar_paquete_bajo_lock(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> RadicacionPrepResultado:
    """Used by the EXISTING synchronous `POST /paquete/regenerar` (via
    `paquete_service.regenerar_paquete`) — see this module's docstring,
    Option B, for why the endpoint's response shape stays unchanged while
    still sharing the per-cuenta lock with the new async path.

    Raises `PaqueteGenerationInProgressError` (409) instead of running the
    pipeline when another run (sync OR async) is already in flight for this
    cuenta — never runs the expensive pipeline twice in parallel, never
    silently blocks.
    """
    cuenta = await paquete_service._get_cuenta_con_contrato_vivo(db, usuario_id, cuenta_id)
    try:
        _job, should_enqueue = await _upsert_job(db, cuenta_id)
    except UpsertPaqueteJobRetryExhaustedError as exc:
        raise NotFoundError("CuentaCobro", str(cuenta_id)) from exc
    if not should_enqueue:
        raise PaqueteGenerationInProgressError(cuenta_id)
    return await _ejecutar_generacion_paquete(db, usuario_id, cuenta.id)


async def obtener_estado_paquete_job(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> PaqueteJobResponse:
    """`GET /paquete/job` — poll the current/last job state. No job ever
    triggered yet -> synthetic `pending` response with every result field
    `None` (mirrors `evidence_classification_service.
    obtener_estado_clasificacion`'s "no job yet" default)."""
    await paquete_service._get_cuenta_con_contrato_vivo(db, usuario_id, cuenta_id)
    result = await db.execute(select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_id))
    job = result.scalar_one_or_none()
    if job is None:
        return PaqueteJobResponse(cuenta_cobro_id=cuenta_id, status=EstadoPaqueteJob.PENDING.value)
    return PaqueteJobResponse.model_validate(job)
