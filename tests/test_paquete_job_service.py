"""Package generation as a poll-based background job, per-cuenta lock
(radicacion-sin-friccion, Phase 2 slice 2.7).

Mirrors `tests/test_evidence_classification_concurrency.py` and
`tests/test_evidence_classification_background_session.py`'s exact
deterministic-reproduction techniques — see those modules' docstrings for
the full rationale (`asyncio.gather` of two overlapping `client.post()` calls
against this suite's SQLite `StaticPool` corrupts the OTHER concurrently-open
session's pending work; a genuinely separate, separately-committed session
standing in for "another request finished moments earlier" is used instead).

Covers (see `app.services.paquete_job_service`'s module docstring for the
Option A/B design decision these tests prove):
- async-vs-async concurrent regenerate for the same cuenta -> exactly ONE
  pipeline run scheduled.
- sync-vs-sync concurrent regenerate for the same cuenta -> the loser gets
  `PaqueteGenerationInProgressError` (409), never a duplicate pipeline run.
- sync-vs-async concurrent regenerate for the same cuenta -> same guarantee,
  whichever direction loses.
- a genuinely stale (crashed) job is reset and re-triggerable, not falsely
  locked forever.
- first-ever trigger phantom-insert race — retried and recovers.
- a failure partway through the pipeline leaves nothing orphaned in storage
  and the job row correctly reaches `failed` with a real error, never stuck.
- the background-task session-sharing safety property, proven again for
  THIS endpoint's own wiring (not just cited by analogy to `ClasificacionJob`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.adapters.storage.port import StorageObjectInfo
from app.core.config import settings
from app.core.exceptions import PaqueteGenerationInProgressError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.paquete_job import EstadoPaqueteJob, PaqueteJob
from app.services import informe_service, paquete_job_service, paquete_service
from fastapi import BackgroundTasks
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import async_session_test

pytestmark = pytest.mark.asyncio

_CODIGOS_OBLIGATORIOS = [
    "CONTRATO",
    "RPC",
    "SEGURIDAD_SOCIAL",
    "INFORME_ACTIVIDADES",
    "INFORME_SUPERVISION",
    "EVIDENCIAS",
    "CEDULA",
    "RUT",
    "ACTA_INICIO",
]


class _FakeStorage:
    """Stateful in-memory `StoragePort` fake (same shape as `test_paquete_
    endpoints.py`'s own) — a plain `AsyncMock` can't assert "no orphaned
    object" (needs real key tracking)."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._objects[key] = data
        return key

    async def download(self, key: str) -> bytes:
        return self._objects[key]

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return f"fake://{key}"

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def list_objects(self, prefix: str) -> list[StorageObjectInfo]:
        return [StorageObjectInfo(key=k, size_bytes=len(v)) for k, v in self._objects.items() if k.startswith(prefix)]

    async def stat(self, key: str) -> StorageObjectInfo | None:
        data = self._objects.get(key)
        return StorageObjectInfo(key=key, size_bytes=len(data)) if data is not None else None


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PAQ-JOB-001",
        objeto="Servicios profesionales para pruebas de paquete job",
        valor_total=24_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual de prueba con texto suficientemente largo",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    await db.refresh(contrato)
    return ob


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _completar_checklist(client: AsyncClient, headers: dict[str, str], cuenta_id: Any) -> None:
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist", headers=headers)
    assert r.status_code == 200, r.text
    for codigo in _CODIGOS_OBLIGATORIOS:
        p = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/{codigo}",
            headers=headers,
            json={"cumplido_manual": True},
        )
        assert p.status_code == 200, p.text


@pytest.fixture
async def cuenta_lista(
    db: AsyncSession,
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligacion: Obligacion,
) -> CuentaCobro:
    """A cuenta whose checklist is fully complete and which has at least one
    evidencia on its only obligación (LISTO/PENDIENTE reports LISTO) — the
    pipeline `preparar_radicacion` runs to a real `done` outcome."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada",
        justificacion="Justificación",
        fecha_realizacion=date(2024, 1, 10),
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)

    ev = Evidencia(
        actividad_id=act.id,
        storage_key=f"evidencias/{act.id}/foto.jpg",
        nombre_archivo="foto.jpg",
        tipo_archivo="image/jpeg",
        tamano_bytes=12,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    await db.refresh(cuenta)
    return cuenta


async def _job_rows(db: AsyncSession, cuenta_id: Any) -> list[PaqueteJob]:
    rows = await db.execute(select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_id))
    return list(rows.scalars().all())


# ── async-vs-async: exactly one background run scheduled ────────────────────


async def test_concurrent_regenerar_async_same_cuenta_schedules_background_run_exactly_once(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETERMINISTIC technique (not `asyncio.gather`, see module docstring):
    `paquete_service._get_cuenta_con_contrato_vivo` (the call `encolar_
    generacion_paquete` makes immediately before `_upsert_job`) is patched so
    that, on its FIRST invocation only, a genuinely separate session runs a
    full concurrent-winner `encolar_generacion_paquete` + executes ITS
    scheduled background task to completion — standing in for another
    request's whole request/response cycle finishing moments before ours
    reaches `_upsert_job`."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(paquete_job_service, "_ejecutar_generacion_paquete_background", spy)

    real_check = paquete_service._get_cuenta_con_contrato_vivo
    injected = {"done": False}

    async def _maybe_inject_winner_then_real(db_: AsyncSession, usuario_id: Any, cuenta_id: Any) -> Any:
        if not injected["done"]:
            injected["done"] = True
            async with async_session_test() as otra_sesion:
                otra_bg = BackgroundTasks()
                await paquete_job_service.encolar_generacion_paquete(otra_sesion, otra_bg, usuario_id, cuenta_id)
                await otra_bg()  # run the "winner" request's own scheduled background task
        return await real_check(db_, usuario_id, cuenta_id)

    monkeypatch.setattr(paquete_service, "_get_cuenta_con_contrato_vivo", _maybe_inject_winner_then_real)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/regenerar-async", headers=test_user["headers"]
    )
    assert resp.status_code == 202, resp.text

    assert spy.await_count == 1  # only the winner's run executed — our own call was a true no-op
    assert len(await _job_rows(db, cuenta_lista.id)) == 1  # one job row per cuenta, never duplicated


# ── sync-vs-sync: the loser gets a clean 409, never a duplicate run ─────────


async def test_concurrent_regenerar_sync_same_cuenta_loser_gets_409_not_duplicate_run(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping `POST /paquete/regenerar` (the EXISTING synchronous
    endpoint) calls for the same cuenta: the second must NOT re-run the
    expensive pipeline in parallel — it fails fast with
    `PaqueteGenerationInProgressError` (409, `PAQUETE_GENERACION_EN_CURSO`),
    per this slice's Option B design decision (see `paquete_job_service`'s
    module docstring).

    The injected "winner" only ACQUIRES the lock (via `_upsert_job`, leaving
    the job `running`) rather than running the full pipeline to completion —
    a winner that ran to completion first would leave the job `done` (not
    `pending`/`running`), which is no longer "in flight" and would legitimately
    let a later caller start a fresh run instead of proving genuine overlap."""
    cuenta_id = cuenta_lista.id  # plain value up front — see MissingGreenlet trap note below
    real_check = paquete_service._get_cuenta_con_contrato_vivo
    injected = {"done": False}

    async def _maybe_inject_winner_then_real(db_: AsyncSession, usuario_id: Any, cid: Any) -> Any:
        if not injected["done"]:
            injected["done"] = True
            async with async_session_test() as otra_sesion:
                job, should_enqueue = await paquete_job_service._upsert_job(otra_sesion, cid)
                assert should_enqueue is True
                job.status = EstadoPaqueteJob.RUNNING.value
                await otra_sesion.commit()
        return await real_check(db_, usuario_id, cid)

    monkeypatch.setattr(paquete_service, "_get_cuenta_con_contrato_vivo", _maybe_inject_winner_then_real)

    # `_upsert_job`'s rollback-on-retry path (not hit here, but `db.commit()`
    # inside the winner's OWN separate session doesn't touch `db` — still,
    # `cuenta_lista` was fetched on `db` earlier, so read only the plain
    # `cuenta_id` captured above from here on, not `cuenta_lista` itself
    # (same MissingGreenlet trap documented in
    # `test_evidence_classification_concurrency.py`).
    with pytest.raises(PaqueteGenerationInProgressError) as exc_info:
        await paquete_job_service.generar_paquete_bajo_lock(db, test_user["user"].id, cuenta_id)

    assert exc_info.value.code == "PAQUETE_GENERACION_EN_CURSO"
    assert len(await _job_rows(db, cuenta_id)) == 1  # still one job row — the loser never created/duplicated one


# ── sync-vs-async: same lock, either direction ───────────────────────────────


async def test_concurrent_regenerar_sync_vs_async_same_cuenta_async_loser_is_a_no_op(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync `POST /paquete/regenerar` call already holds the lock (fresh
    `pending`/`running` job) -> a concurrent ASYNC `encolar_generacion_
    paquete` for the same cuenta must observe that in-flight job and return
    `should_enqueue=False` (verified indirectly: zero NEW background tasks
    scheduled), never scheduling a second parallel pipeline run."""
    storage = _FakeStorage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    real_check = paquete_service._get_cuenta_con_contrato_vivo
    injected = {"done": False}
    bg_from_async_caller = BackgroundTasks()

    async def _maybe_inject_sync_winner_then_real(db_: AsyncSession, usuario_id: Any, cuenta_id: Any) -> Any:
        if not injected["done"]:
            injected["done"] = True
            async with async_session_test() as otra_sesion:
                # The "winner" acquires the lock and marks the job `running`
                # (but never finishes within this window) — mirrors a real
                # in-flight sync request racing our async trigger.
                job, should_enqueue = await paquete_job_service._upsert_job(otra_sesion, cuenta_id)
                assert should_enqueue is True
                job.status = EstadoPaqueteJob.RUNNING.value
                await otra_sesion.commit()
        return await real_check(db_, usuario_id, cuenta_id)

    monkeypatch.setattr(paquete_service, "_get_cuenta_con_contrato_vivo", _maybe_inject_sync_winner_then_real)

    job = await paquete_job_service.encolar_generacion_paquete(
        db, bg_from_async_caller, test_user["user"].id, cuenta_lista.id
    )

    assert len(bg_from_async_caller.tasks) == 0  # no-op: the async caller never scheduled a second run
    assert job.status == EstadoPaqueteJob.RUNNING.value  # observed the sync winner's in-flight state
    assert len(await _job_rows(db, cuenta_lista.id)) == 1


# ── a genuinely stale job is reset and re-triggerable ────────────────────────


async def test_stale_running_paquete_job_is_reset_and_reenqueued_under_lock(
    db: AsyncSession, test_user: dict[str, Any], cuenta_lista: CuentaCobro
) -> None:
    job = PaqueteJob(cuenta_cobro_id=cuenta_lista.id, status=EstadoPaqueteJob.RUNNING.value)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    stale_time = datetime.now(UTC) - timedelta(seconds=settings.PAQUETE_JOB_STALE_SECONDS + 30)
    await db.execute(update(PaqueteJob).where(PaqueteJob.id == job.id).values(updated_at=stale_time))
    await db.commit()

    bg = BackgroundTasks()
    result = await paquete_job_service.encolar_generacion_paquete(db, bg, test_user["user"].id, cuenta_lista.id)

    assert len(bg.tasks) == 1
    assert result.status == EstadoPaqueteJob.PENDING.value


# ── stale-pending reset actually bumps updated_at (BLOCKER regression) ──────


async def test_stale_pending_paquete_job_reset_is_not_re_stale_for_a_second_caller(
    db: AsyncSession, test_user: dict[str, Any], cuenta_lista: CuentaCobro
) -> None:
    """Regression for a real BLOCKER a review found: SQLAlchemy's dirty-
    checking (`History.from_scalar_attribute`) compares old vs new with `==`
    and emits NO UPDATE when an attribute is reassigned to the value it
    already holds. A stale `pending` row already has `error`/`error_code` ==
    `None`, so `_upsert_job`'s reset-branch assignments to those two fields
    (plus reassigning `status` to the SAME `pending` value) are all no-ops
    for this exact status -- before the fix, `updated_at` never moved, so a
    second caller re-reading the same still-stale `age_seconds` moments
    later would ALSO conclude the row is stale and ALSO "win" the lock,
    scheduling a second pipeline run for what should be a single reset.

    Unlike the stale-RUNNING test above (which only proves the reset
    happens), this proves the reset is actually OBSERVED as fresh by a
    second, genuinely separate caller immediately afterward -- the exact
    gap the review flagged as untested.
    """
    job = PaqueteJob(cuenta_cobro_id=cuenta_lista.id, status=EstadoPaqueteJob.PENDING.value)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    stale_time = datetime.now(UTC) - timedelta(seconds=settings.PAQUETE_JOB_STALE_SECONDS + 30)
    await db.execute(update(PaqueteJob).where(PaqueteJob.id == job.id).values(updated_at=stale_time))
    await db.commit()

    bg_winner = BackgroundTasks()
    winner = await paquete_job_service.encolar_generacion_paquete(db, bg_winner, test_user["user"].id, cuenta_lista.id)
    assert len(bg_winner.tasks) == 1  # first caller correctly resets the stale row and wins

    async with async_session_test() as otra_sesion:
        bg_second = BackgroundTasks()
        second = await paquete_job_service.encolar_generacion_paquete(
            otra_sesion, bg_second, test_user["user"].id, cuenta_lista.id
        )
        # The row the winner just reset is now FRESH (updated_at bumped to
        # "now"), so a second caller checking it moments later must observe
        # it as in-flight and idempotent-no-op -- NOT stale again.
        assert len(bg_second.tasks) == 0
        assert second.id == winner.id
        assert len(await _job_rows(otra_sesion, cuenta_lista.id)) == 1  # never duplicated


# ── first-ever trigger, same cuenta: phantom-insert race retry-and-recover ──


async def test_concurrent_first_trigger_same_cuenta_phantom_insert_retries_and_recovers(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same technique as `test_evidence_classification_concurrency.py`'s
    identical test for `ClasificacionJob` — see that test's docstring for the
    full REPAIR NOTE on why the winner must be injected AFTER the plain
    SELECT (observing `None`) but BEFORE the INSERT commits."""
    cuenta_id = cuenta_lista.id

    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(paquete_job_service, "_ejecutar_generacion_paquete_background", spy)

    real_execute = db.execute
    injected = {"done": False}

    async def _execute_then_maybe_inject_winner(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        result = await real_execute(stmt, *args, **kwargs)
        if not injected["done"] and PaqueteJob.__table__.name in str(stmt):
            injected["done"] = True
            async with async_session_test() as otra_sesion:
                otra_bg = BackgroundTasks()
                await paquete_job_service.encolar_generacion_paquete(
                    otra_sesion, otra_bg, test_user["user"].id, cuenta_id
                )
                await otra_bg()
        return result

    monkeypatch.setattr(db, "execute", _execute_then_maybe_inject_winner)

    job = await paquete_job_service.encolar_generacion_paquete(db, BackgroundTasks(), test_user["user"].id, cuenta_id)
    assert job is not None  # retry-and-recover: no raised exception

    assert spy.await_count == 1  # only the winner's run executed
    assert len(await _job_rows(db, cuenta_id)) == 1  # no duplicate, no orphan from the loser's rollback


# ── a failure partway through the pipeline leaves nothing orphaned ──────────


async def test_pipeline_failure_marks_job_failed_with_no_orphaned_storage_object(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`radicacion_prep_service.preparar_radicacion` only calls `storage.
    upload(...)` AFTER `informe_service.generar_zip_evidencias` has fully
    succeeded (the ZIP is built entirely in memory first) — so a failure
    inside the packager must leave the storage backend completely empty, and
    the job row must reach `failed` with the real error message, never stuck
    `running`. Uses a plain (non-`DomainError`) exception to prove the
    fail-open generic-exception path too, not just the already-covered
    `DomainError` gate propagation (`test_paquete_endpoints.py`)."""
    # Plain values up front (`_marcar_fallo`'s `db.rollback()` expires every
    # attribute SQLAlchemy is tracking on `db`, including `cuenta_lista` —
    # touching `cuenta_lista.id` AFTER that raises `MissingGreenlet` outside
    # an awaited context; same trap `test_evidence_classification_
    # concurrency.py`'s module docstring documents).
    usuario_id = test_user["user"].id
    cuenta_id = cuenta_lista.id

    storage = _FakeStorage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    async def _boom(*_a: Any, **_k: Any) -> tuple[bytes, str]:
        raise RuntimeError("fallo simulado a mitad de la generación del paquete")

    monkeypatch.setattr(informe_service, "generar_zip_evidencias", _boom)

    with pytest.raises(RuntimeError, match="fallo simulado"):
        await paquete_job_service.generar_paquete_bajo_lock(db, usuario_id, cuenta_id)

    prefix = f"paquetes/{usuario_id}/{cuenta_id}/"
    assert await storage.list_objects(prefix) == []  # nothing orphaned

    job_result = await db.execute(select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_id))
    job = job_result.scalar_one()
    assert job.status == EstadoPaqueteJob.FAILED.value
    assert job.error is not None and "fallo simulado" in job.error
    assert job.error_code is None  # not a DomainError, so no structured code — still recorded, not silently lost


# ── background-task session-sharing safety property, for THIS endpoint ──────


async def test_background_paquete_generation_runs_to_completion_via_real_endpoint(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same empirical proof as `test_evidence_classification_background_
    session.py`, re-proven for THIS endpoint's own wiring (not assumed by
    analogy) — the real `POST .../paquete/regenerar-async` endpoint through
    the app's actual `get_db` injection reaches `done` with a real persisted
    package, proving `_ejecutar_generacion_paquete` ran to completion against
    a still-open, usable session under FastAPI's real `BackgroundTasks` +
    `Depends(get_db)` interaction."""
    storage = _FakeStorage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/regenerar-async", headers=test_user["headers"]
    )
    assert resp.status_code == 202, resp.text

    # By the time client.post() returns, FastAPI's BackgroundTasks have
    # already run to completion (see module docstring / the classification
    # job's identical proof) — a closed-session race would leave the job
    # stuck `pending`/`running` or surface a 500 on the endpoint call itself.
    job_result = await db.execute(select(PaqueteJob).where(PaqueteJob.cuenta_cobro_id == cuenta_lista.id))
    job = job_result.scalar_one()
    assert job.status == EstadoPaqueteJob.DONE.value
    assert job.storage_key is not None
    assert job.listo_para_radicar is True

    objetos = await storage.list_objects(f"paquetes/{test_user['user'].id}/{cuenta_lista.id}/")
    assert len(objetos) == 1  # a real object was persisted, not a smoke-test-only 202

    r_job = await client.get(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/job", headers=test_user["headers"])
    assert r_job.status_code == 200, r_job.text
    body = r_job.json()
    assert body["status"] == "done"
    assert body["storage_key"] == job.storage_key
    assert body["es_borrador"] is True


async def test_get_paquete_job_returns_pending_default_when_never_triggered(
    client: AsyncClient, test_user: dict[str, Any], cuenta_lista: CuentaCobro
) -> None:
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/job", headers=test_user["headers"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["storage_key"] is None
    assert body["cuenta_cobro_id"] == str(cuenta_lista.id)
