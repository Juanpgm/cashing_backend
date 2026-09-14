"""Concurrency tests for the batch-upload phase of `subir_evidencias_cuenta`
(radicacion-sin-friccion Phase 2 slice 2.6): uploads for genuinely-new files
in one batch must run concurrently, bounded by `_UPLOAD_CONCURRENCY` (8), not
disguised-sequential `await`s in a loop.

Mirrors the timing-based technique already established in
`tests/test_cuenta_cobro_service.py::test_generar_pdf_runs_off_event_loop`
(mocked slow I/O + a scheduled tracker coroutine, asserting the tracker's
ticks land close to schedule instead of being pushed out by serialization).
"""

from __future__ import annotations

import asyncio
import time as time_module
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.services import evidencia_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _crear_contrato(db: AsyncSession, usuario_id: Any, numero: str) -> Contrato:
    c = Contrato(
        usuario_id=usuario_id,
        numero_contrato=numero,
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="SENA",
        dependencia="Sistemas",
        supervisor_nombre="Pedro",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _crear_cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=3_000_000)
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    return await _crear_contrato(db, test_user["user"].id, "CTR-UPLOAD-CONC-001")


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    return await _crear_cuenta(db, contrato)


async def test_uploads_genuinely_concurrent_not_disguised_sequential(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """8 distinct files, each upload mocked to take 0.15s (blocking-style via
    asyncio.sleep standing in for slow I/O): if uploads ran sequentially this
    would take >= 1.2s; genuinely concurrent (bounded by the semaphore, which
    allows all 8 in flight at once here) it should take close to 0.15s.

    A concurrently-scheduled tracker coroutine (same technique as
    `test_generar_pdf_runs_off_event_loop`) proves the event loop kept
    ticking on schedule WHILE uploads were in flight — not just that the
    wall-clock total happened to be short.
    """
    user = test_user["user"]
    storage = AsyncMock()
    storage.presigned_url = AsyncMock(return_value="https://s3.example.com/presigned")

    upload_delay = 0.15

    async def _slow_upload(*, key: str, data: bytes, content_type: str) -> str:
        await asyncio.sleep(upload_delay)
        return key

    storage.upload = AsyncMock(side_effect=_slow_upload)

    tracker_ticks: list[float] = []

    async def _tracker() -> None:
        loop = asyncio.get_event_loop()
        start = loop.time()
        for _ in range(10):
            await asyncio.sleep(0.02)
            tracker_ticks.append(loop.time() - start)

    archivos = [(f"archivo{i}.txt", "text/plain", f"contenido distinto numero {i}".encode()) for i in range(8)]

    start = time_module.perf_counter()
    _resultados, _ = await asyncio.gather(
        evidencia_service.subir_evidencias_cuenta(
            db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id, archivos=archivos
        ),
        _tracker(),
    )
    elapsed = time_module.perf_counter() - start

    assert storage.upload.call_count == 8
    # Sequential would be >= 8 * 0.15s = 1.2s; concurrent (all 8 fit under the
    # semaphore(8) bound) should land close to one delay's worth.
    assert elapsed < 0.6, f"uploads took {elapsed:.3f}s — looks sequential, not concurrent"
    # The tracker's ticks should land close to their own schedule (~0.20s
    # total for 10 * 0.02s) — if uploads blocked the loop, later ticks would
    # be pushed out well past that window.
    assert tracker_ticks[-1] < 0.5, (
        f"tracker was delayed past the concurrent upload window — got {tracker_ticks[-1]:.3f}s"
    )


async def test_uploads_bounded_by_semaphore_of_eight(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """12 distinct files (> the semaphore(8) bound): peak concurrent in-flight
    uploads must never exceed 8 — proving the concurrency is BOUNDED, not
    unbounded `asyncio.gather` over every file at once."""
    user = test_user["user"]
    storage = AsyncMock()
    storage.presigned_url = AsyncMock(return_value="https://s3.example.com/presigned")

    in_flight = 0
    peak_in_flight = 0
    lock = asyncio.Lock()

    async def _tracked_upload(*, key: str, data: bytes, content_type: str) -> str:
        nonlocal in_flight, peak_in_flight
        async with lock:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return key

    storage.upload = AsyncMock(side_effect=_tracked_upload)

    archivos = [(f"archivo{i}.txt", "text/plain", f"contenido distinto numero {i}".encode()) for i in range(12)]

    await evidencia_service.subir_evidencias_cuenta(
        db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id, archivos=archivos
    )

    assert storage.upload.call_count == 12
    assert peak_in_flight == evidencia_service._UPLOAD_CONCURRENCY
    assert peak_in_flight <= 8
