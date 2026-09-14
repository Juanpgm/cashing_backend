"""Regression tests for radicacion-sin-friccion slice 2.4a: flipping
`Contrato.cuentas_cobro`, `Obligacion.actividades`, and `CuentaCobro.borradores`
from `lazy="selectin"` to `lazy="raise_on_sql"` (see `app/models/contrato.py`,
`app/models/obligacion.py`, `app/models/cuenta_cobro.py`).

An audit of `app/` found NO production code anywhere that reads any of these
three relationship attributes directly (`contrato.cuentas_cobro`,
`obligacion.actividades`, `cuenta.borradores`) — every real call site already
queries the child model explicitly (`select(CuentaCobro).where(contrato_id=...)`,
`select(Actividad).where(...)`, `select(BorradorCuentaCobro).where(...)`), so
flipping the flag broke zero production call sites. Confirmed by running the
full suite after the flip: only ONE test broke
(`tests/test_agente_cadena_completa_chat_loop.py::
test_realistic_multi_turn_chat_loop_threads_tool_calls_to_a_real_radicar_cuenta`,
fixed in that same file) — and that break was NOT a direct relationship
access at all, it was a stale Python object reference that used to survive a
`db.rollback()` only because `Contrato.cuentas_cobro`'s old `lazy="selectin"`
default silently re-populated it as a side effect of an unrelated
`db.refresh(contrato)` call. See `test_refreshing_contrato_no_longer_
silently_unexpires_its_cuentas_cobro` below, which pins that exact mechanism.

This file exists to (a) pin the new loud-failure contract for all three
relationships so nobody accidentally reverts to `lazy="selectin"`, (b) prove
the documented `selectinload(...)` escape hatch still works, and (c) measure
the real query-count reduction via `QueryCounter` (same methodology as
`tests/test_query_budgets.py`).
"""

from __future__ import annotations

import pytest
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from sqlalchemy import inspect, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.conftest import QueryCounter, async_session_test
from tests.factories import ActividadFactory, ContratoFactory, CuentaCobroFactory

pytestmark = pytest.mark.asyncio


# ── Contrato.cuentas_cobro ───────────────────────────────────────────────


async def test_contrato_cuentas_cobro_raises_on_unadorned_access(db: AsyncSession) -> None:
    """A plain, unadorned access to `Contrato.cuentas_cobro` on an object
    loaded WITHOUT an explicit `selectinload` must raise loud
    (`InvalidRequestError`), never silently emit an extra query. Loaded via a
    FRESH session (not the identity-mapped `contrato` used to create the row)
    so the access genuinely needs real SQL — `raise_on_sql` only raises when
    it would emit a query."""
    contrato = await ContratoFactory.create_async(db)
    await CuentaCobroFactory.create_async(db, contrato=contrato)
    await db.commit()
    contrato_id = contrato.id

    async with async_session_test() as fresh_db:
        loaded = (await fresh_db.execute(select(Contrato).where(Contrato.id == contrato_id))).scalar_one()
        with pytest.raises(InvalidRequestError, match="raise_on_sql"):
            _ = loaded.cuentas_cobro


async def test_contrato_cuentas_cobro_loads_cleanly_with_explicit_selectinload(db: AsyncSession) -> None:
    """The documented escape hatch works: `.options(selectinload(...))` loads
    the collection without raising."""
    contrato = await ContratoFactory.create_async(db)
    cuenta = await CuentaCobroFactory.create_async(db, contrato=contrato)
    await db.commit()
    contrato_id = contrato.id

    async with async_session_test() as fresh_db:
        loaded = (
            await fresh_db.execute(
                select(Contrato).options(selectinload(Contrato.cuentas_cobro)).where(Contrato.id == contrato_id)
            )
        ).scalar_one()
        assert [c.id for c in loaded.cuentas_cobro] == [cuenta.id]


async def test_contrato_load_no_longer_cascades_an_extra_query_for_cuentas_cobro(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    """Query-count regression: `Contrato.obligaciones` is UNCHANGED by this
    slice (still `lazy="selectin"`), so a bare `select(Contrato)` still costs
    2 statements (main row + the obligaciones cascade) — this pins that only
    the SECOND statement (the old `cuentas_cobro` cascade) is gone, not the
    whole eager-loading behavior of the model. Before this slice, the same
    plain `select(Contrato)` cost 3: main row + obligaciones cascade +
    cuentas_cobro cascade (the latter was the class-level DEFAULT strategy,
    no `.options()` needed to trigger it — nobody asked for it)."""
    contrato = await ContratoFactory.create_async(db)
    await CuentaCobroFactory.create_async(db, contrato=contrato)
    await db.commit()
    contrato_id = contrato.id

    async with async_session_test() as fresh_db:
        query_counter.reset()
        (await fresh_db.execute(select(Contrato).where(Contrato.id == contrato_id))).scalar_one()
        assert query_counter.count == 2, (
            f"Expected exactly 2 queries loading a bare Contrato (main row + the still-eager "
            f"`obligaciones` cascade), got {query_counter.count}: {query_counter.statements}"
        )


async def test_refreshing_contrato_no_longer_silently_unexpires_its_cuentas_cobro(db: AsyncSession) -> None:
    """Pins the EXACT mechanism behind the one real breakage this slice
    surfaced (`tests/test_agente_cadena_completa_chat_loop.py`'s
    `test_realistic_multi_turn_chat_loop_threads_tool_calls_to_a_real_radicar_cuenta`,
    fixed in that same file). Before this slice, `Contrato.cuentas_cobro`'s
    `lazy="selectin"` was the class-level DEFAULT eager-load strategy, so ANY
    query that touched a `Contrato` — including `Session.refresh()` — silently
    re-issued `SELECT cuentas_cobro WHERE contrato_id = ...` as a side effect,
    which (via the identity map) ALSO refreshed/un-expired any already-loaded
    `CuentaCobro` row for that contrato, even though nothing asked for it.
    That accidental refresh is gone: refreshing an expired `Contrato` must NOT
    resurrect an expired sibling `CuentaCobro`'s scalar attributes."""
    contrato = await ContratoFactory.create_async(db)
    cuenta = await CuentaCobroFactory.create_async(db, contrato=contrato)
    await db.commit()

    # A bare commit with no further activity leaves no open transaction for
    # rollback() to invalidate — mirror the REAL production trigger instead
    # (a query that begins a new transaction, then a failure that rolls it
    # back), same shape as `_execute_tool_once`'s try/query/except/rollback.
    await db.execute(select(Contrato).where(Contrato.id == contrato.id))
    await db.rollback()  # expires every object in the session, `cuenta` included
    assert "id" in inspect(cuenta).expired_attributes

    await db.refresh(contrato)  # must NOT touch `cuenta` as a side effect anymore

    assert "id" in inspect(cuenta).expired_attributes, (
        "cuenta.id should still be expired after refreshing an unrelated contrato — "
        "if this fails, Contrato.cuentas_cobro is cascading (eagerly re-loading) again"
    )


# ── Obligacion.actividades ───────────────────────────────────────────────


async def test_obligacion_actividades_raises_on_unadorned_access(db: AsyncSession) -> None:
    contrato = await ContratoFactory.create_async(db)
    cuenta = await CuentaCobroFactory.create_async(db, contrato=contrato)
    obligacion = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación de prueba",
        tipo=TipoObligacion.ESPECIFICA,
        orden=0,
        etiqueta="OE1",
    )
    db.add(obligacion)
    await db.flush()
    await ActividadFactory.create_async(db, cuenta_cobro=cuenta, obligacion_id=obligacion.id)
    await db.commit()
    obligacion_id = obligacion.id

    async with async_session_test() as fresh_db:
        loaded = (await fresh_db.execute(select(Obligacion).where(Obligacion.id == obligacion_id))).scalar_one()
        with pytest.raises(InvalidRequestError, match="raise_on_sql"):
            _ = loaded.actividades


async def test_obligacion_actividades_loads_cleanly_with_explicit_selectinload(db: AsyncSession) -> None:
    contrato = await ContratoFactory.create_async(db)
    cuenta = await CuentaCobroFactory.create_async(db, contrato=contrato)
    obligacion = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación de prueba",
        tipo=TipoObligacion.ESPECIFICA,
        orden=0,
        etiqueta="OE1",
    )
    db.add(obligacion)
    await db.flush()
    actividad = await ActividadFactory.create_async(db, cuenta_cobro=cuenta, obligacion_id=obligacion.id)
    await db.commit()
    obligacion_id = obligacion.id

    async with async_session_test() as fresh_db:
        loaded = (
            await fresh_db.execute(
                select(Obligacion).options(selectinload(Obligacion.actividades)).where(Obligacion.id == obligacion_id)
            )
        ).scalar_one()
        assert [a.id for a in loaded.actividades] == [actividad.id]


async def test_obligacion_load_no_longer_cascades_an_extra_query_for_actividades(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    """Same regression as the Contrato case above, for `Obligacion.actividades`."""
    contrato = await ContratoFactory.create_async(db)
    cuenta = await CuentaCobroFactory.create_async(db, contrato=contrato)
    obligacion = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación de prueba",
        tipo=TipoObligacion.ESPECIFICA,
        orden=0,
        etiqueta="OE1",
    )
    db.add(obligacion)
    await db.flush()
    await ActividadFactory.create_async(db, cuenta_cobro=cuenta, obligacion_id=obligacion.id)
    await db.commit()
    obligacion_id = obligacion.id

    async with async_session_test() as fresh_db:
        query_counter.reset()
        (await fresh_db.execute(select(Obligacion).where(Obligacion.id == obligacion_id))).scalar_one()
        assert query_counter.count == 1, (
            f"Expected exactly 1 query loading a bare Obligacion, got {query_counter.count}: {query_counter.statements}"
        )


# ── CuentaCobro.borradores ───────────────────────────────────────────────


async def test_cuenta_cobro_borradores_raises_on_unadorned_access(db: AsyncSession) -> None:
    cuenta = await CuentaCobroFactory.create_async(db)
    db.add(BorradorCuentaCobro(cuenta_cobro_id=cuenta.id, version=1, contenido={"foo": "bar"}))
    await db.commit()
    cuenta_id = cuenta.id

    async with async_session_test() as fresh_db:
        loaded = (await fresh_db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))).scalar_one()
        with pytest.raises(InvalidRequestError, match="raise_on_sql"):
            _ = loaded.borradores


async def test_cuenta_cobro_borradores_loads_cleanly_with_explicit_selectinload(db: AsyncSession) -> None:
    cuenta = await CuentaCobroFactory.create_async(db)
    borrador = BorradorCuentaCobro(cuenta_cobro_id=cuenta.id, version=1, contenido={"foo": "bar"})
    db.add(borrador)
    await db.commit()
    cuenta_id = cuenta.id
    borrador_id = borrador.id

    async with async_session_test() as fresh_db:
        loaded = (
            await fresh_db.execute(
                select(CuentaCobro).options(selectinload(CuentaCobro.borradores)).where(CuentaCobro.id == cuenta_id)
            )
        ).scalar_one()
        assert [b.id for b in loaded.borradores] == [borrador_id]


async def test_cuenta_cobro_load_no_longer_cascades_an_extra_query_for_borradores(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    """`CuentaCobro.actividades` is UNCHANGED by this slice (still
    `lazy="selectin"`), so a bare `select(CuentaCobro)` still costs 2
    statements (main row + the actividades cascade) — this pins that only the
    THIRD statement (the old `borradores` cascade) is gone, not the whole
    eager-loading behavior of the model."""
    cuenta = await CuentaCobroFactory.create_async(db)
    db.add(BorradorCuentaCobro(cuenta_cobro_id=cuenta.id, version=1, contenido={"foo": "bar"}))
    await db.commit()
    cuenta_id = cuenta.id

    async with async_session_test() as fresh_db:
        query_counter.reset()
        (await fresh_db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))).scalar_one()
        assert query_counter.count == 2, (
            f"Expected exactly 2 queries loading a bare CuentaCobro (main row + the still-eager "
            f"`actividades` cascade), got {query_counter.count}: {query_counter.statements}"
        )
