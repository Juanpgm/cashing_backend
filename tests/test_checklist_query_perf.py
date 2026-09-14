"""Query-count regression tests for radicacion-sin-friccion slice 2.5 (cheap
checklist perf wins): `construir_checklist_completo` reusing an already-loaded
`cuenta.contrato` instead of re-querying it, and `listar_catalogo` caching its
result per-`AsyncSession` instead of re-querying/re-seeding on every call
within the same request.

Both tests measure real statement counts via `QueryCounter` (see
`tests/conftest.py`), not estimates — same methodology as
`tests/test_query_budgets.py`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.requisito_documento import RequisitoDocumento
from app.services import checklist_service
from sqlalchemy import inspect, select
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.conftest import QueryCounter, async_session_test

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CHK-PERF-001",
        objeto="Servicios de checklist perf",
        valor_total=12_000_000,
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
async def cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── construir_checklist_completo: reuse already-loaded cuenta.contrato ─────


async def test_construir_checklist_completo_reuses_preloaded_contrato_no_extra_query(
    db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro, query_counter: QueryCounter
) -> None:
    """When the caller already eager-loaded `cuenta.contrato` (the real shape
    every production call site uses, via `_get_cuenta_con_ownership`'s
    `selectinload(CuentaCobro.contrato)`), `construir_checklist_completo` must
    NOT issue a second `SELECT ... FROM contratos` to fetch the same row again."""
    res = await db.execute(
        select(CuentaCobro).options(selectinload(CuentaCobro.contrato)).where(CuentaCobro.id == cuenta.id)
    )
    cuenta_preloaded = res.scalar_one()
    assert "contrato" not in inspect(cuenta_preloaded).unloaded

    query_counter.reset()
    await checklist_service.construir_checklist_completo(db, cuenta_preloaded)

    contrato_selects = [s for s in query_counter.statements if "FROM contratos" in s]
    assert contrato_selects == [], (
        f"Expected zero redundant 'FROM contratos' queries when cuenta.contrato is "
        f"already loaded, got {len(contrato_selects)}: {contrato_selects}"
    )


async def test_construir_checklist_completo_still_works_without_preloaded_contrato(
    db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    """Defensive fallback: a caller that passes a `cuenta` whose `.contrato` is
    NOT already loaded (e.g. most direct unit-test call sites in this suite,
    which build `cuenta` in the same session as `contrato` without an explicit
    `selectinload`) must still get a correct payload — the redundant-query fix
    must fall back to an explicit query instead of crashing or returning wrong
    data when the relationship isn't eagerly loaded."""
    payload = await checklist_service.construir_checklist_completo(db, cuenta)

    assert payload["cuenta_cobro_id"] == cuenta.id
    assert len(payload["items"]) > 0


async def test_construir_checklist_completo_across_fresh_session_without_preload_does_not_crash(
    contrato: Contrato, cuenta: CuentaCobro
) -> None:
    """Real production shape for the *fallback* path: a brand-new `AsyncSession`
    (fresh identity map, so `cuenta.contrato` cannot resolve via the identity-map
    `use_get` shortcut) that loads `cuenta` WITHOUT eager-loading `contrato`.
    Must fall back to the explicit query rather than raising
    `sqlalchemy.exc.MissingGreenlet` (confirmed by direct probe: implicit lazy
    loading across a fresh async session raises, it does not silently do one
    query)."""
    async with async_session_test() as fresh_db:
        res = await fresh_db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta.id))
        cuenta_bare = res.scalar_one()
        assert "contrato" in inspect(cuenta_bare).unloaded

        payload = await checklist_service.construir_checklist_completo(fresh_db, cuenta_bare)

        assert payload["cuenta_cobro_id"] == cuenta.id


async def test_construir_checklist_completo_pre_existing_expired_cuenta_limitation_unchanged(
    db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    """Pins a PRE-EXISTING limitation of `construir_checklist_completo` that
    this slice's contrato-reuse fix does NOT change (confirmed by reproducing
    the identical failure against clean master, before this slice's changes):
    reusing a `cuenta` object AFTER `db.rollback()` WITHOUT re-fetching it
    fresh raises `MissingGreenlet` — `db.rollback()` expires every attribute
    on every persistent object, including plain columns like `cuenta.contrato_id`
    (which the ORIGINAL, pre-slice-2.5 code already dereferenced to build its
    `Contrato` query), not just relationships. Every real call site avoids this
    by re-fetching `cuenta` fresh after a rollback (see
    `app/api/v1/checklist.py::refrescar_secop`'s except-block) before calling
    this function again — this test documents that requirement still holds
    and that the contrato-reuse optimization does not make it any worse."""
    res = await db.execute(
        select(CuentaCobro).options(selectinload(CuentaCobro.contrato)).where(CuentaCobro.id == cuenta.id)
    )
    cuenta_preloaded = res.scalar_one()

    await db.rollback()

    with pytest.raises(MissingGreenlet):
        await checklist_service.construir_checklist_completo(db, cuenta_preloaded)


# ── listar_catalogo: per-session cache ──────────────────────────────────────


async def test_listar_catalogo_reuses_cache_within_same_session(db: AsyncSession, query_counter: QueryCounter) -> None:
    """Two calls to `listar_catalogo` on the SAME `AsyncSession` must issue
    real SQL only on the first call — the second call is served from a
    per-session cache (safe because nothing besides `listar_catalogo`'s own
    seed step ever mutates `requisito_documento`, and the cache dies with the
    session, so it can never outlive the DB state it was computed from)."""
    query_counter.reset()
    catalogo1 = await checklist_service.listar_catalogo(db)
    first_call_count = query_counter.count
    assert first_call_count > 0

    query_counter.reset()
    catalogo2 = await checklist_service.listar_catalogo(db)

    assert query_counter.count == 0, (
        f"Expected the second listar_catalogo() call on the same session to hit zero "
        f"queries (cache hit), got {query_counter.count}: {query_counter.statements}"
    )
    assert [c.codigo for c in catalogo1] == [c.codigo for c in catalogo2]


async def test_listar_catalogo_cache_is_session_scoped_not_stale_across_sessions(
    test_user: dict[str, Any],
) -> None:
    """A cache scoped to one `AsyncSession` must never leak into another
    session — each fresh session (matching this test suite's per-test schema
    drop/recreate, and a fresh production request) must independently observe
    the true current DB state, including running the seed/backfill step, not
    a stale Python-object snapshot from an unrelated session."""
    async with async_session_test() as db1:
        catalogo1 = await checklist_service.listar_catalogo(db1)
        codigos1 = {c.codigo for c in catalogo1}
        assert "CDP" in codigos1

    async with async_session_test() as db2:
        # A fresh session must run its own seed-check/query, not reuse db1's
        # cached instances (which are bound to db1's now-closed session).
        catalogo2 = await checklist_service.listar_catalogo(db2)
        codigos2 = {c.codigo for c in catalogo2}
        assert codigos2 == codigos1
        # Prove these are genuinely this session's own objects, not db1's stale
        # instances: attribute access must work without needing db1 at all.
        for item in catalogo2:
            assert item.codigo is not None


async def test_listar_catalogo_cache_survives_a_mid_session_rollback(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    """Regression guard for a real bug this cache introduced and this same
    slice fixed: `db.rollback()` does NOT clear `db.info` (where the cache
    lives), but it DOES expire every persistent object in the session,
    including cached `RequisitoDocumento` rows — a naive cache that just
    returns whatever is in `db.info` unconditionally would hand back expired
    instances, and any later synchronous attribute access on them (e.g.
    `asegurar_checklist`'s `{req.codigo for req in catalogo}`) raises
    `sqlalchemy.exc.MissingGreenlet` instead of transparently refreshing —
    confirmed via a direct repro against `/refresh-secop`'s real
    pass-level-exception-then-rollback flow (see
    `test_checklist_scan_secop.py::test_refresh_secop_endpoint_returns_200_and_rolls_back_on_pass_level_exception`).

    After a rollback, `listar_catalogo` must treat its cache as invalidated
    and safely rebuild it — not raise, and not return stale/expired rows."""
    catalogo1 = await checklist_service.listar_catalogo(db)
    assert len(catalogo1) > 0
    # Access every attribute the cache promises is safe to read, pre-rollback.
    codigos1 = {c.codigo for c in catalogo1}

    await db.rollback()

    query_counter.reset()
    catalogo2 = await checklist_service.listar_catalogo(db)

    # Must have actually rebuilt (real queries issued), not returned the now-
    # expired cached instances.
    assert query_counter.count > 0
    codigos2 = {c.codigo for c in catalogo2}
    assert codigos2 == codigos1


async def test_listar_catalogo_additive_backfill_still_idempotent_with_cache(
    db: AsyncSession,
) -> None:
    """Existing coverage (`test_catalogo_seed_insertar_si_ausente_cuando_catalogo_ya_sembrado`
    in test_checklist_cdp.py) already proves backfill idempotency across two
    same-session calls; this test pins the same invariant explicitly against
    the cached implementation: seeding a catalog WITHOUT one code, then calling
    `listar_catalogo` twice, must never produce a duplicate row for that code —
    the cache must reflect the post-backfill state on call #1, and stay
    consistent (same objects) on call #2."""
    for item in checklist_service._CATALOGO_SEED:
        if item["codigo"] == "CDP":
            continue
        db.add(RequisitoDocumento(**item))
    await db.commit()

    catalogo1 = await checklist_service.listar_catalogo(db)
    assert [c.codigo for c in catalogo1].count("CDP") == 1

    catalogo2 = await checklist_service.listar_catalogo(db)
    assert [c.codigo for c in catalogo2].count("CDP") == 1
