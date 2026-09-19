"""Concurrency test suite (radicacion-sin-friccion, Phase 4, slice 4.2):
double classification-job enqueue for the same `cuenta_id` (RES-002).

Two concurrent `encolar_clasificacion` calls for the same cuenta must schedule
the actual background run (`_ejecutar_clasificacion`) exactly ONCE — the
second caller must see the first caller's already-committed
`PENDING`/`RUNNING` row and return `should_enqueue=False`.

TECHNIQUE: real HTTP requests via `asyncio.gather` (mirrors
`test_radicar_idempotente.py::test_radicar_concurrente_una_sola_transicion`) —
FastAPI's native `BackgroundTasks` run to completion INSIDE the awaited
request/response cycle under `httpx.ASGITransport` (in-process, no real
network), so by the time both `client.post()` calls return, every background
task that WAS scheduled has already run. `_ejecutar_clasificacion` is
replaced with a spy so we can count exactly how many times the background
work was actually scheduled/run, independent of its own (unrelated) logic.

HONESTY NOTE: `SELECT ... FOR UPDATE` is a no-op on this suite's SQLite test
engine (`tests/conftest.py`) — same caveat as `cuenta_cobro_service.
_leer_estado_bajo_lock` (slice 1.2). These tests prove the CODE PATH is
race-safe under whatever interleaving asyncio/the shared-connection SQLite
pool produce here; the row lock only becomes a REAL guarantee under Postgres
(`scripts/test-postgres.sh`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.config import settings
from app.models.actividad import Actividad
from app.models.clasificacion_job import ClasificacionEvidenciasJob, EstadoClasificacionJob
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.services import evidence_classification_service
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import async_session_test

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CLAS-CONC-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(contrato_id=contrato.id, mes=3, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1)
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def cuenta_cobro_b(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1)
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def actividad_stub(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(cuenta_cobro_id=cuenta_cobro.id, descripcion="Evidencias sin clasificar")
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@pytest.fixture
async def actividad_stub_b(db: AsyncSession, cuenta_cobro_b: CuentaCobro) -> Actividad:
    a = Actividad(cuenta_cobro_id=cuenta_cobro_b.id, descripcion="Evidencias sin clasificar")
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _crear_evidencia(db: AsyncSession, actividad: Actividad, nombre: str, texto: str) -> Evidencia:
    ev = Evidencia(
        actividad_id=actividad.id,
        storage_key=f"evidencias/test/{nombre}",
        nombre_archivo=nombre,
        tipo_archivo="text/plain",
        tamano_bytes=len(texto),
        texto_extraido=texto,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return ev


# ── Same cuenta_id: exactly one background run scheduled ────────────────────


async def test_concurrent_encolar_same_cuenta_schedules_background_run_exactly_once(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    actividad_stub: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETERMINISTIC technique (not `asyncio.gather` — see module docstring): a
    real `asyncio.gather` of two `client.post()` calls WAS tried here first and
    does reproduce the underlying TOCTOU race, but `_upsert_job`'s rollback-and-
    retry path (needed for the phantom-insert race below) corrupts the OTHER
    concurrently-open session's pending work on this suite's SQLite
    `StaticPool` shared connection — a test-harness artifact, not a production
    bug (see `test_cuenta_cobro_concurrency.py`'s module docstring for the same
    finding). Instead: `_evidencias_sin_clasificar` (the call `encolar_
    clasificacion` makes immediately before `_upsert_job`) is patched so that,
    on its FIRST invocation only, a genuinely separate session runs a full
    concurrent-winner `encolar_clasificacion` + executes ITS scheduled
    background task to completion — standing in for another request's whole
    request/response cycle finishing moments before ours reaches `_upsert_job`.
    """
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(evidence_classification_service, "_ejecutar_clasificacion", spy)

    real_evidencias_sin_clasificar = evidence_classification_service._evidencias_sin_clasificar
    injected = {"done": False}

    async def _maybe_inject_winner_then_real(db_: AsyncSession, cuenta_id: Any) -> list[Evidencia]:
        if not injected["done"]:
            injected["done"] = True
            from fastapi import BackgroundTasks

            async with async_session_test() as otra_sesion:
                otra_bg = BackgroundTasks()
                await evidence_classification_service.encolar_clasificacion(otra_sesion, otra_bg, cuenta_id)
                await otra_bg()  # run the "winner" request's own scheduled background task
        return await real_evidencias_sin_clasificar(db_, cuenta_id)

    monkeypatch.setattr(evidence_classification_service, "_evidencias_sin_clasificar", _maybe_inject_winner_then_real)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/clasificar", headers=test_user["headers"]
    )
    assert resp.status_code == 202, resp.text

    assert spy.await_count == 1  # only the winner's run executed — our own call was a true no-op

    rows = (
        (
            await db.execute(
                select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_cobro.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # one job row per cuenta, never duplicated


# ── First-ever trigger, same cuenta: phantom-insert race retry-and-recover ──


async def test_concurrent_first_trigger_same_cuenta_phantom_insert_retries_and_recovers(
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    actividad_stub: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_upsert_job`'s phantom-insert race: on a cuenta's FIRST-EVER trigger
    there is no existing row for either caller to `SELECT ... FOR UPDATE` on,
    so both concurrent callers' plain SELECT returns `None` and both attempt
    an INSERT — the loser collides on `uq_clasificacion_job_cuenta`, catches
    `IntegrityError`, rolls back, and retries once (see `_upsert_job`'s
    docstring, "A FIRST-EVER trigger" bullet). Zero test coverage for this
    path before this test despite the docstring devoting a full paragraph to
    it (radicacion-sin-friccion slice 4.2 concurrency audit, WARNING finding).

    TECHNIQUE: same deterministic reproduction idiom as this module's other
    tests and `test_cuenta_cobro_concurrency.py` — a genuinely separate
    session commits the "winning" `ClasificacionEvidenciasJob` row for the
    SAME brand-new `cuenta_id` FIRST, fully committed. The injection point is
    narrower than the "same cuenta" test above though: that test's winner
    must land BEFORE our own SELECT (to hit the existing-row/lock branch);
    this one's winner must land AFTER our own plain SELECT already observed
    `None` but BEFORE our own INSERT commits (to hit the phantom-insert
    branch instead) — so `db.execute` itself is wrapped to inject the winner
    immediately after intercepting the specific SELECT against
    `clasificacion_evidencias_job` returns its (still-`None`) result, and
    before that result is handed back to `_upsert_job`.

    REPAIR NOTE (round-3 review, mutation-tested): this test used to drive the
    race through a real `client.post(...)` HTTP call while monkeypatching
    THIS test's `db` fixture. That never actually raced anything — the real
    request goes through `tests/conftest.py`'s `_override_get_db`, which opens
    its OWN separate `async_session_test()` per request, a DIFFERENT
    `AsyncSession` object than the one this test patches. So the injected
    "winner" commit landed AFTER the assertions instead of racing them
    (proven by mutation testing: deleting `_upsert_job`'s
    `except IntegrityError` handler outright left this test green). Fixed by
    calling `encolar_clasificacion(db, ...)` DIRECTLY — same idiom this
    file's other direct-call tests already use (see
    `test_upsert_job_retry_exhausted_is_remapped_to_not_found_not_raw_500` and
    `test_stale_running_job_is_reset_and_reenqueued_under_lock`) — so the
    `db` session this test patches is genuinely the one `_upsert_job`
    executes against. Trade-off: no more HTTP `202` assertion (there's no
    longer an HTTP call to make one), which is fine — what actually matters
    is the service-level contract this test proves: no-raise + exactly-
    one-scheduled-run + exactly-one-row, all asserted below.
    """
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    # Plain value up front: `_upsert_job`'s `rollback()` on the losing path
    # expires EVERY ORM object tracked by `db` (not just the ones it wrote) —
    # including `cuenta_cobro`/`actividad_stub` — so any later attribute
    # access on them would trigger an implicit lazy-reload outside an awaited
    # context (`MissingGreenlet`). Same idiom as
    # `test_cuenta_cobro_concurrency.py::test_crear_cuenta_cobro_race_loser_gets_clean_error_not_raw_500`.
    cuenta_id = cuenta_cobro.id

    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(evidence_classification_service, "_ejecutar_clasificacion", spy)

    real_execute = db.execute
    injected = {"done": False}

    async def _execute_then_maybe_inject_winner(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        result = await real_execute(stmt, *args, **kwargs)
        if not injected["done"] and ClasificacionEvidenciasJob.__tablename__ in str(stmt):
            injected["done"] = True
            from fastapi import BackgroundTasks

            async with async_session_test() as otra_sesion:
                otra_bg = BackgroundTasks()
                await evidence_classification_service.encolar_clasificacion(otra_sesion, otra_bg, cuenta_id)
                await otra_bg()  # run the "winner" request's own scheduled background task
        return result

    monkeypatch.setattr(db, "execute", _execute_then_maybe_inject_winner)

    from fastapi import BackgroundTasks

    # Direct service call (not client.post — see REPAIR NOTE above): this is
    # the SAME `db` session `_execute_then_maybe_inject_winner` patches, so
    # the phantom-insert race is genuinely reproduced.
    job = await evidence_classification_service.encolar_clasificacion(db, BackgroundTasks(), cuenta_id)
    assert job is not None  # retry-and-recover: no raised exception

    # Exactly one background task scheduled across the two logical callers: the
    # winner's own run (spy call #1) — our own call lost the phantom-insert
    # race, retried, observed the winner's just-committed fresh PENDING row on
    # the locked-update branch, and correctly no-op'd (should_enqueue=False).
    assert spy.await_count == 1

    rows = (
        (
            await db.execute(
                select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # only the winner's row exists — no duplicate, no orphan from the loser's rollback


# ── `_upsert_job`'s exhausted-retry error maps to a clean 404 ───────────────


async def test_upsert_job_retry_exhausted_is_remapped_to_not_found_not_raw_500(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SERVICE-LEVEL contract only: `encolar_clasificacion` must remap
    `_upsert_job`'s `UpsertJobRetryExhaustedError` (its 2-attempt retry loop
    exhausted — practically unreachable via a pure unique-constraint race,
    see `_upsert_job`'s docstring) into a clean `NotFoundError`, never let it
    surface as a raw, unhandled exception. This test calls
    `encolar_clasificacion` directly and asserts on the raised exception
    type — it does NOT exercise `POST /{cuenta_id}/evidencias/clasificar`
    end-to-end, so it does not by itself prove the endpoint returns HTTP 404
    rather than 500 (that mapping is `NotFoundError` → 404 via
    `app/core/exceptions.py`'s `EXCEPTION_STATUS_MAP`, exercised generically
    elsewhere, not re-proven here).

    NOTE on reproduction scope: the realistic trigger for
    `UpsertJobRetryExhaustedError` (per `encolar_clasificacion`'s docstring)
    is the cuenta being hard-deleted concurrently mid-retry, turning the
    second INSERT attempt's collision into an FK violation instead of the
    unique-constraint race the retry loop already proves it resolves in <=2
    attempts (see the phantom-insert test above). Deterministically
    reproducing THAT exact interleaving would need injecting a real
    concurrent hard-delete between `_upsert_job`'s two internal retry
    attempts, which is impractical to construct reliably in this suite. This
    test instead directly proves the boundary-remapping contract in
    `encolar_clasificacion` — that ANY `UpsertJobRetryExhaustedError` out of
    `_upsert_job` becomes a clean `NotFoundError`, regardless of what
    specifically triggered it — by forcing `_upsert_job` to raise it. It also
    implicitly proves the remap is narrow: since `_always_collides` raises
    `UpsertJobRetryExhaustedError` (not a bare `RuntimeError`), this only
    passes if `encolar_clasificacion`'s `except` clause matches that specific
    type.
    """
    from app.core.exceptions import NotFoundError
    from app.services.evidence_classification_service import UpsertJobRetryExhaustedError

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    async def _always_collides(db_: AsyncSession, cuenta_id: Any, total: int) -> Any:
        raise UpsertJobRetryExhaustedError(f"_upsert_job: persistent insert collision for cuenta_cobro_id={cuenta_id}")

    monkeypatch.setattr(evidence_classification_service, "_upsert_job", _always_collides)

    from fastapi import BackgroundTasks

    with pytest.raises(NotFoundError) as exc_info:
        await evidence_classification_service.encolar_clasificacion(db, BackgroundTasks(), cuenta_cobro.id)
    assert str(cuenta_cobro.id) in exc_info.value.detail


# ── Different cuenta_ids: both proceed independently, no false contention ───


async def test_concurrent_encolar_different_cuentas_both_schedule_independently(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    cuenta_cobro_b: CuentaCobro,
    actividad_stub: Actividad,
    actividad_stub_b: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    await _crear_evidencia(db, actividad_stub_b, "b.txt", "Informe tecnico mensual de asesoria")

    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(evidence_classification_service, "_ejecutar_clasificacion", spy)

    r1, r2 = await asyncio.gather(
        client.post(f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/clasificar", headers=test_user["headers"]),
        client.post(f"/api/v1/cuentas-cobro/{cuenta_cobro_b.id}/evidencias/clasificar", headers=test_user["headers"]),
    )
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text

    assert spy.await_count == 2  # both cuentas scheduled their own independent run — no false contention


# ── Stale `running` job is still correctly resettable under the new lock ────


async def test_stale_running_job_is_reset_and_reenqueued_under_lock(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """The `SELECT ... FOR UPDATE` guard must NOT accidentally treat a stale
    `running` job as permanently locked — it's still resettable exactly like
    before the lock was added (RES-002 staleness recovery)."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    job = ClasificacionEvidenciasJob(
        cuenta_cobro_id=cuenta_cobro.id, status=EstadoClasificacionJob.RUNNING.value, total=5, procesadas=2
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    stale_time = datetime.now(UTC) - timedelta(seconds=settings.EVIDENCE_JOB_STALE_SECONDS + 30)
    await db.execute(
        update(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.id == job.id).values(updated_at=stale_time)
    )
    await db.commit()

    bg = BackgroundTasks()
    result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

    assert len(bg.tasks) == 1
    assert result.status == EstadoClasificacionJob.PENDING.value
    assert result.total == 1


# ── Stale `pending` reset must actually bump updated_at (WARNING-6 regression) ─
#
# SQLAlchemy's dirty-checking emits NO UPDATE when every assigned attribute
# equals its current value, so column-level `onupdate=func.now()` never fires.
# A stale PENDING row with the same `total`, `procesadas == 0` and `error is
# None` is exactly that case: without an explicit `updated_at` bump its
# staleness clock never resets and a second caller also "wins" the reset.


async def _seed_job(
    db: AsyncSession,
    cuenta_id: Any,
    *,
    status: str,
    total: int,
    procesadas: int = 0,
    error: str | None = None,
    updated_at: datetime,
) -> ClasificacionEvidenciasJob:
    job = ClasificacionEvidenciasJob(
        cuenta_cobro_id=cuenta_id, status=status, total=total, procesadas=procesadas, error=error
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await db.execute(
        update(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.id == job.id).values(updated_at=updated_at)
    )
    await db.commit()
    return job


def _stale_time() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=settings.EVIDENCE_JOB_STALE_SECONDS + 30)


async def _job_rows(db: AsyncSession, cuenta_id: Any) -> list[ClasificacionEvidenciasJob]:
    result = await db.execute(
        select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
    )
    return list(result.scalars().all())


async def test_stale_pending_job_noop_reset_is_not_re_stale_for_a_second_caller(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """(a) Stale PENDING with the SAME total, procesadas == 0 and error None:
    every reset assignment is a no-op for SQLAlchemy, so only an explicit
    `updated_at` bump makes the reset visible. A second, genuinely separate
    session calling right after must see the row as fresh and NOT re-enqueue."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    job = await _seed_job(
        db,
        cuenta_cobro.id,
        status=EstadoClasificacionJob.PENDING.value,
        total=1,  # equals the pending evidence count -> no attribute changes
        updated_at=_stale_time(),
    )

    bg_winner = BackgroundTasks()
    winner = await evidence_classification_service.encolar_clasificacion(db, bg_winner, cuenta_cobro.id)
    assert len(bg_winner.tasks) == 1  # first caller resets the stale row and wins
    assert winner.id == job.id

    async with async_session_test() as otra_sesion:
        bg_second = BackgroundTasks()
        second = await evidence_classification_service.encolar_clasificacion(otra_sesion, bg_second, cuenta_cobro.id)
        assert len(bg_second.tasks) == 0  # row is fresh now -> idempotent no-op
        assert second.id == winner.id
        assert len(await _job_rows(otra_sesion, cuenta_cobro.id)) == 1  # never duplicated


async def test_stale_pending_job_reset_moves_updated_at_forward(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """The reset must move `updated_at` to (about) now, not leave the stale value."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    stale = _stale_time()
    await _seed_job(db, cuenta_cobro.id, status=EstadoClasificacionJob.PENDING.value, total=1, updated_at=stale)

    result = await evidence_classification_service.encolar_clasificacion(db, BackgroundTasks(), cuenta_cobro.id)

    new_updated_at = result.updated_at
    if new_updated_at.tzinfo is None:
        new_updated_at = new_updated_at.replace(tzinfo=UTC)
    assert (datetime.now(UTC) - new_updated_at).total_seconds() < settings.EVIDENCE_JOB_STALE_SECONDS
    assert new_updated_at > stale


class _FrozenDatetime(datetime):
    """`datetime` whose `now()` is pinned, so the staleness boundary is exact."""

    frozen: datetime

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return cls.frozen if tz is None else cls.frozen.astimezone(tz)


@pytest.mark.parametrize("status", [EstadoClasificacionJob.PENDING.value, EstadoClasificacionJob.RUNNING.value])
@pytest.mark.parametrize(
    ("offset_seconds", "expect_reset"),
    [
        (settings.EVIDENCE_JOB_STALE_SECONDS - 0.001, False),  # just inside the window -> fresh
        (settings.EVIDENCE_JOB_STALE_SECONDS, True),  # exactly at the threshold -> stale (`<` is strict)
        (settings.EVIDENCE_JOB_STALE_SECONDS + 0.001, True),  # just past -> stale
    ],
    ids=["just_inside", "exactly_at", "just_past"],
)
async def test_stale_threshold_boundary_for_pending_and_running(
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    actividad_stub: Actividad,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    offset_seconds: float,
    expect_reset: bool,
) -> None:
    """(b) Boundary of EVIDENCE_JOB_STALE_SECONDS with time frozen. A job aged
    exactly the threshold is stale; strictly younger is a no-op."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    frozen_now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    _FrozenDatetime.frozen = frozen_now
    monkeypatch.setattr(evidence_classification_service, "datetime", _FrozenDatetime)
    await _seed_job(
        db,
        cuenta_cobro.id,
        status=status,
        total=1,
        updated_at=frozen_now - timedelta(seconds=offset_seconds),
    )

    bg = BackgroundTasks()
    result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

    assert (len(bg.tasks) == 1) is expect_reset
    if expect_reset:
        assert result.status == EstadoClasificacionJob.PENDING.value
    else:
        assert result.status == status  # untouched


async def test_stale_pending_job_with_different_total_is_reset_and_reenqueued(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """(c) A stale PENDING whose `total` differs already emitted an UPDATE
    before the fix -- it must keep working and stay fresh for a second caller."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    await _seed_job(db, cuenta_cobro.id, status=EstadoClasificacionJob.PENDING.value, total=7, updated_at=_stale_time())

    bg = BackgroundTasks()
    result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)
    assert len(bg.tasks) == 1
    assert result.status == EstadoClasificacionJob.PENDING.value
    assert result.total == 1

    async with async_session_test() as otra_sesion:
        bg_second = BackgroundTasks()
        await evidence_classification_service.encolar_clasificacion(otra_sesion, bg_second, cuenta_cobro.id)
        assert len(bg_second.tasks) == 0


async def test_stale_job_with_error_set_is_reset_and_error_cleared(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """(d) A stale job carrying a previous error: reset clears it."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    await _seed_job(
        db,
        cuenta_cobro.id,
        status=EstadoClasificacionJob.PENDING.value,
        total=1,
        error="boom: previous run crashed",
        updated_at=_stale_time(),
    )

    bg = BackgroundTasks()
    result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

    assert len(bg.tasks) == 1
    assert result.error is None
    assert result.procesadas == 0
    assert result.status == EstadoClasificacionJob.PENDING.value


@pytest.mark.parametrize("status", [EstadoClasificacionJob.PENDING.value, EstadoClasificacionJob.RUNNING.value])
async def test_fresh_pending_or_running_job_is_not_reset(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad, status: str
) -> None:
    """(e) A fresh in-flight job is an idempotent no-op: nothing enqueued and
    progress / error left untouched."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    job = await _seed_job(db, cuenta_cobro.id, status=status, total=9, procesadas=4, error="keep", updated_at=fresh)

    bg = BackgroundTasks()
    result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

    assert len(bg.tasks) == 0
    assert result.id == job.id
    assert result.status == status
    assert result.total == 9
    assert result.procesadas == 4
    assert result.error == "keep"


@pytest.mark.parametrize("status", [EstadoClasificacionJob.DONE.value, EstadoClasificacionJob.FAILED.value])
async def test_terminal_job_is_reset_and_reenqueued_regardless_of_age(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad, status: str
) -> None:
    """(f) Terminal states (done/failed) are always re-triggerable, even when
    fresh (existing semantics -- must not regress), and the reset is fresh for
    a second caller."""
    from fastapi import BackgroundTasks

    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
    await _seed_job(
        db,
        cuenta_cobro.id,
        status=status,
        total=3,
        procesadas=3,
        error="old" if status == EstadoClasificacionJob.FAILED.value else None,
        updated_at=datetime.now(UTC),
    )

    bg = BackgroundTasks()
    result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

    assert len(bg.tasks) == 1
    assert result.status == EstadoClasificacionJob.PENDING.value
    assert result.total == 1
    assert result.procesadas == 0
    assert result.error is None

    async with async_session_test() as otra_sesion:
        bg_second = BackgroundTasks()
        await evidence_classification_service.encolar_clasificacion(otra_sesion, bg_second, cuenta_cobro.id)
        assert len(bg_second.tasks) == 0
    assert result.procesadas == 0


# ── Identity-map staleness under interleaving (review round 2, CRITICAL-1) ──
#
# `_upsert_job` first SELECTs the job (plain), then re-SELECTs it `FOR UPDATE`.
# Both statements return the SAME identity-map instance, and SQLAlchemy does
# not overwrite the attributes of an already-loaded, non-expired instance with
# the fresh row unless `populate_existing` is set. So a caller B that loaded the
# row BEFORE a winner A reset+committed it keeps the STALE `updated_at`/`status`
# in memory, even after waiting on A's row lock — and also "wins" the reset
# (duplicate enqueue). This reproduces on SQLite without any real lock: it is
# purely an identity-map defect.


async def _interleave_winner_then_loser(
    db: AsyncSession,
    cuenta_id: Any,
    *,
    seed_status: str,
    seed_total: int,
    winner_finishes_job: bool = False,
    winner_acts: bool = True,
) -> tuple[ClasificacionEvidenciasJob, int, ClasificacionEvidenciasJob]:
    """Genuine two-session interleaving.

    1. Seed a STALE job (via `db`, a third session).
    2. Loser session B loads the job with a plain SELECT FIRST (caches the stale
       row in ITS identity map) — asserted below so the test cannot be vacuous.
    3. Winner session A runs `encolar_clasificacion` (reset path) and commits;
       optionally also drives the job to DONE, as its background run would.
    4. B now calls `encolar_clasificacion` on the SAME session that cached the
       stale row.

    Returns `(job_B_returned, tasks_scheduled_by_B, job_B_cached_before_call)`.
    """
    from fastapi import BackgroundTasks

    await _seed_job(db, cuenta_id, status=seed_status, total=seed_total, updated_at=_stale_time())

    async with async_session_test() as sesion_b:
        cached = (
            await sesion_b.execute(
                select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
            )
        ).scalar_one()
        # Guard against a tautological test: B really holds the STALE row.
        assert cached.status == seed_status
        assert (
            datetime.now(UTC) - cached.updated_at.replace(tzinfo=cached.updated_at.tzinfo or UTC)
        ).total_seconds() >= (settings.EVIDENCE_JOB_STALE_SECONDS)

        if winner_acts:
            async with async_session_test() as sesion_a:
                bg_a = BackgroundTasks()
                await evidence_classification_service.encolar_clasificacion(sesion_a, bg_a, cuenta_id)
                assert len(bg_a.tasks) == 1  # A is the legitimate winner of the stale reset
                if winner_finishes_job:
                    await sesion_a.execute(
                        update(ClasificacionEvidenciasJob)
                        .where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
                        .values(status=EstadoClasificacionJob.DONE.value, procesadas=1)
                    )
                    await sesion_a.commit()

        bg_b = BackgroundTasks()
        returned = await evidence_classification_service.encolar_clasificacion(sesion_b, bg_b, cuenta_id)
        return returned, len(bg_b.tasks), cached


@pytest.mark.parametrize("seed_status", [EstadoClasificacionJob.PENDING.value, EstadoClasificacionJob.RUNNING.value])
async def test_loser_with_stale_identity_map_sees_winners_fresh_reset_and_does_not_enqueue(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad, seed_status: str
) -> None:
    """Stale PENDING with the same total (the WARNING-6 shape) and stale RUNNING:
    B read the stale row BEFORE A reset+committed it. B must see A's fresh
    `updated_at` after the lock and NOT schedule a duplicate run."""
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    returned, scheduled_by_b, _ = await _interleave_winner_then_loser(
        db, cuenta_cobro.id, seed_status=seed_status, seed_total=1
    )

    assert scheduled_by_b == 0  # duplicate enqueue if B trusts its stale cached row
    assert returned.status == EstadoClasificacionJob.PENDING.value
    assert (
        datetime.now(UTC) - returned.updated_at.replace(tzinfo=returned.updated_at.tzinfo or UTC)
    ).total_seconds() < (settings.EVIDENCE_JOB_STALE_SECONDS)


async def test_loser_with_stale_identity_map_sees_winners_changed_total(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """A's reset also changed `total` (7 -> 1). B must observe A's committed
    values, not its cached `total == 7`, and must not enqueue."""
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    returned, scheduled_by_b, cached = await _interleave_winner_then_loser(
        db, cuenta_cobro.id, seed_status=EstadoClasificacionJob.PENDING.value, seed_total=7
    )

    assert scheduled_by_b == 0
    assert returned is cached  # same identity-map instance, now refreshed in place
    assert returned.total == 1


async def test_loser_with_stale_identity_map_after_winner_finished_job_resets_from_fresh_state(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """A reset the job AND its run already finished it (DONE, procesadas=1).
    DONE is terminal, so per existing semantics B may re-trigger it — but it
    must do so from the FRESH row: with a stale identity map B's `procesadas = 0`
    assignment is a no-op against its cached 0, so no UPDATE is emitted and the
    row stays `done` in the DB while a run is scheduled for it (lost update)."""
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    returned, scheduled_by_b, _ = await _interleave_winner_then_loser(
        db,
        cuenta_cobro.id,
        seed_status=EstadoClasificacionJob.PENDING.value,
        seed_total=1,
        winner_finishes_job=True,
    )

    assert scheduled_by_b == 1  # DONE is terminal -> re-triggerable (existing semantics)
    assert returned.status == EstadoClasificacionJob.PENDING.value
    assert returned.procesadas == 0
    assert returned.total == 1


async def test_loser_is_still_reset_when_row_is_genuinely_stale_and_winner_did_nothing(
    db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
) -> None:
    """Control: `populate_existing` must not over-suppress. If no winner touched
    the row and it is genuinely stale, B still resets and enqueues exactly once."""
    await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

    returned, scheduled_by_b, _ = await _interleave_winner_then_loser(
        db,
        cuenta_cobro.id,
        seed_status=EstadoClasificacionJob.PENDING.value,
        seed_total=1,
        winner_acts=False,
    )

    assert scheduled_by_b == 1
    assert returned.status == EstadoClasificacionJob.PENDING.value
