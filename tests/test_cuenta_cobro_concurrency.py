"""Concurrency test suite (radicacion-sin-friccion, Phase 4, slice 4.2):
double `crear_cuenta_cobro` for the same `(contrato_id, mes, anio)`.

`test_radicar_idempotente.py::test_radicar_concurrente_una_sola_transicion`
(slice 1.2) already covers double `radicar` — not duplicated here.

HONESTY NOTE on technique: an `asyncio.gather` of two real HTTP requests (the
technique `test_radicar_idempotente.py` uses) was tried first here and does
reproduce the underlying TOCTOU race on this suite's SQLite `StaticPool` test
engine (`tests/conftest.py`) — but `crear_cuenta_cobro` (unlike `radicar_cuenta`)
does MULTIPLE writes (tombstone delete, credit deduct, `Credito` insert, cuenta
insert) that are all still uncommitted when the race resolves, and StaticPool's
single shared physical connection means one session's `rollback()` corrupts the
OTHER session's concurrently-pending, still-uncommitted writes too (both
"sessions" are really the same DBAPI connection) — producing a spurious 500
(`Could not refresh instance`) on the WINNER side that has nothing to do with
the production fix. `radicar_cuenta`'s CAS is a single atomic `UPDATE ... WHERE`
with no earlier uncommitted writes in the same request, so it doesn't hit this
harness limitation.

The same-`(mes, anio)` tests below instead use the DETERMINISTIC reproduction
technique already established in `test_radicar_idempotente.py::
test_radicar_short_circuito_post_lock_refleja_estado_de_otra_sesion`: a
genuinely separate session commits the "winning" row FIRST (fully, via its own
`async_session_test()` session — not concurrently-overlapping), and only THEN
does the session under test attempt its own write, which now races against a
REAL, already-committed unique-constraint violation — reproducing the exact
`IntegrityError` a true concurrent winner would cause under Postgres, without
the SQLite/StaticPool cross-session corruption artifact above. This is
narrower than true concurrency but avoids the test-harness hazard: PROVING the
catch-and-convert code path is correct; true multi-connection concurrency is
only real under Postgres (`scripts/test-postgres.sh`).

The DIFFERENT-months test below does NOT hit the corruption issue (both writes
succeed, nothing rolls back), so it keeps the real `asyncio.gather` + HTTP
technique — a strictly stronger proof for that case.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import CUENTA_MES_DUPLICADA, AlreadyExistsError
from app.models.contrato import Contrato
from app.models.credito import Credito
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.usuario import Usuario
from app.schemas.cuenta_cobro import CuentaCobroCreate
from app.services import cuenta_cobro_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import async_session_test

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CONC-001",
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


# ── Same (contrato_id, mes, anio): exactly one winner, clean loser error ────


async def test_crear_cuenta_cobro_race_loser_gets_clean_error_not_raw_500(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent winner commits `(contrato, mes=6, anio=2024)` between our
    session's uniqueness SELECT and its own INSERT — the loser must raise a
    clean `AlreadyExistsError(code=CUENTA_MES_DUPLICADA)`, never a raw
    unhandled `IntegrityError`/500."""
    user = test_user["user"]
    # Plain values up front: the production fix's `db.rollback()` expires
    # EVERY object tracked by `db` (not just the ones it wrote) — including
    # this fixture's `contrato`/`user` — so any later attribute access on them
    # would trigger an implicit lazy-reload outside an awaited context
    # (`MissingGreenlet`). Same idiom as
    # `test_evidence_classification_service.py::TestFalloRetryable`.
    contrato_id, user_id = contrato.id, user.id
    data = CuentaCobroCreate(contrato_id=contrato_id, mes=6, anio=2024, valor=Decimal("3000000.00"))

    real_numero_cuota_siguiente = cuenta_cobro_service._numero_cuota_siguiente

    async def _winner_commits_then_real(db_: AsyncSession, contrato_id: Any) -> int:
        async with async_session_test() as otra_sesion:
            otra_sesion.add(
                CuentaCobro(
                    contrato_id=contrato_id,
                    mes=6,
                    anio=2024,
                    valor=1,
                    estado=EstadoCuentaCobro.BORRADOR,
                    numero_cuota=1,
                    posicion=PosicionCuota.PRIMERA,
                )
            )
            await otra_sesion.commit()
        return await real_numero_cuota_siguiente(db_, contrato_id)

    monkeypatch.setattr(cuenta_cobro_service, "_numero_cuota_siguiente", _winner_commits_then_real)

    with pytest.raises(AlreadyExistsError) as exc_info:
        await cuenta_cobro_service.crear_cuenta_cobro(db, user_id, data)
    assert exc_info.value.code == CUENTA_MES_DUPLICADA

    rows = (
        (
            await db.execute(
                select(CuentaCobro).where(
                    CuentaCobro.contrato_id == contrato_id,
                    CuentaCobro.mes == 6,
                    CuentaCobro.anio == 2024,
                    CuentaCobro.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # only the "winner" row — the loser's INSERT never landed


async def test_crear_cuenta_cobro_race_loser_does_not_get_debited(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loser's whole transaction rolls back — `usuario.creditos_disponibles`
    and the `Credito` ledger show NO debit for the failed attempt (the
    synthetic "winner" here deliberately does NOT touch credits, isolating
    exactly what the loser's own rollback must undo)."""
    user = test_user["user"]
    contrato_id, user_id, creditos_antes = contrato.id, user.id, user.creditos_disponibles
    data = CuentaCobroCreate(contrato_id=contrato_id, mes=7, anio=2024, valor=Decimal("3000000.00"))

    real_numero_cuota_siguiente = cuenta_cobro_service._numero_cuota_siguiente

    async def _winner_commits_then_real(db_: AsyncSession, contrato_id_: Any) -> int:
        async with async_session_test() as otra_sesion:
            otra_sesion.add(
                CuentaCobro(
                    contrato_id=contrato_id_,
                    mes=7,
                    anio=2024,
                    valor=1,
                    estado=EstadoCuentaCobro.BORRADOR,
                    numero_cuota=1,
                    posicion=PosicionCuota.PRIMERA,
                )
            )
            await otra_sesion.commit()
        return await real_numero_cuota_siguiente(db_, contrato_id_)

    monkeypatch.setattr(cuenta_cobro_service, "_numero_cuota_siguiente", _winner_commits_then_real)

    with pytest.raises(AlreadyExistsError):
        await cuenta_cobro_service.crear_cuenta_cobro(db, user_id, data)

    usuario = (await db.execute(select(Usuario).where(Usuario.id == user_id))).scalar_one()
    assert usuario.creditos_disponibles == creditos_antes  # NOT debited — the loser rolled back cleanly

    creditos = (await db.execute(select(Credito).where(Credito.usuario_id == user_id))).scalars().all()
    assert creditos == []  # no stray ledger row from the failed attempt


# ── Different months, same contrato: both succeed at the HTTP level, but ────
# ── numero_cuota/posicion end up CORRUPTED (KNOWN GAP, not a clean pass) ────


async def test_concurrent_crear_cuenta_cobro_different_meses_both_succeed_but_numero_cuota_is_corrupted(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Two concurrent creates for the SAME contrato but DIFFERENT (mes, anio)
    both return 201 — `(contrato_id, mes, anio)` uniqueness is real (backed by
    `uq_contrato_mes_anio`), so there's no false contention on that axis.

    KNOWN GAP (radicacion-sin-friccion slice 4.2 concurrency audit — see the
    matching "KNOWN GAP" comment in `cuenta_cobro_service.crear_cuenta_cobro`):
    `numero_cuota`/`posicion=primera` have NO backing DB constraint, so both
    concurrent requests independently compute "I'm the first cuota" from a
    SELECT-based check that races. This test PROVES the corruption, not a
    clean "no false contention" outcome — do NOT read a passing run here as
    "concurrency is fully safe for this path". If a future migration adds a
    partial unique index on `(contrato_id) WHERE posicion='primera' AND
    deleted_at IS NULL` (the gap's documented fix), this test's assertions
    below will start FAILING and must be updated to assert the corrected,
    non-corrupted state (`numero_cuota` values `{1, 2}`, only one `PRIMERA`).
    """
    payload_a = {"contrato_id": str(contrato.id), "mes": 8, "anio": 2024, "valor": "3000000.00"}
    payload_b = {"contrato_id": str(contrato.id), "mes": 9, "anio": 2024, "valor": "3000000.00"}

    r1, r2 = await asyncio.gather(
        client.post("/api/v1/cuentas-cobro/", json=payload_a, headers=test_user["headers"]),
        client.post("/api/v1/cuentas-cobro/", json=payload_b, headers=test_user["headers"]),
    )

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    rows = (await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))).scalars().all()
    assert {row.mes for row in rows} == {8, 9}

    # Documenting the CURRENT broken reality, not endorsing it: both rows
    # raced past the SELECT-based "am I the first cuota?" check before either
    # INSERTed, so both landed numero_cuota=1/posicion=PRIMERA — a real
    # money/data-integrity gap (billing logic that reads "the first cuota"
    # downstream has two candidates instead of one).
    assert sorted(row.numero_cuota for row in rows) == [1, 1]
    assert [row.posicion for row in rows] == [PosicionCuota.PRIMERA, PosicionCuota.PRIMERA]
