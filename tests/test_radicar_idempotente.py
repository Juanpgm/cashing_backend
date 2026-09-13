"""Tests for idempotent, concurrency-safe `radicar_cuenta` (radicacion-sin-friccion,
slice 1.2).

Before this slice, calling `radicar` twice on the same cuenta raced the
BORRADOR/RECHAZADA gate against the actual state transition: a second caller
(duplicate click, retry, or a genuine concurrent request) would either see a
spurious 422 ("no se puede radicar una cuenta en estado 'enviada'") or, worse,
under concurrent execution, both callers could pass the gate and both run the
coherence/checklist gates against a stale in-memory read before either wrote.

This suite locks the gate behind two mechanisms (see `cuenta_cobro_service`,
W2 follow-up design):
1. `radicar_cuenta` re-reads the row with a PLAIN (unlocked) ownership read at
   the top — an already-ENVIADA cuenta short-circuits BEFORE the
   coherence/checklist gates run at all. Those gates then run UNLOCKED (they
   used to run under a held `FOR UPDATE`, which blocked a concurrent
   transaction's own locking read for the whole ~76-query gate duration and
   pinned pool connections under Postgres). Only immediately before the actual
   transition does `_leer_estado_bajo_lock` re-read JUST the `estado` column
   with `SELECT ... FOR UPDATE`, narrowing the lock window to right before the
   write. Postgres serializes concurrent readers of this statement on the same
   row; SQLite silently ignores the clause (see `secop_service.py`'s existing
   use of the same idiom).
2. `cambiar_estado`'s ENVIADA transition is a compare-and-set bulk UPDATE
   (`WHERE estado IN (borrador, rechazada)`) — only the first writer's UPDATE
   actually matches; a losing concurrent writer sees `rowcount == 0` and
   returns the now-current row instead of raising. This CAS is the actual
   correctness guarantee for the genuine race (both callers observing a
   pre-ENVIADA state before either writes); the lock above only narrows the
   window in which that race can happen.

Combined, an already-ENVIADA cuenta short-circuits to an idempotent 200
BEFORE the coherence/checklist gates run again.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import NotFoundError, ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.services import checklist_service, coherence_validator_service, cuenta_cobro_service
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _IS_PG, async_session_test

pytestmark = pytest.mark.asyncio

# Mandatory (obligatorio=True) requisito codes — same list `test_radicar.py` uses to
# satisfy `computar_resumen`'s radicacion_lista gate.
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


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-RADICAR-IDEMP-001",
        objeto="Servicios profesionales para pruebas de radicación idempotente",
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
    """Cuenta in BORRADOR with the checklist gate already resolved (estandar)."""
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=2,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _completar_checklist(client: AsyncClient, headers: dict[str, str], cuenta_id: uuid.UUID) -> None:
    """Seed the checklist rows and mark every mandatory requisito as cumplido_manual."""
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist", headers=headers)
    assert r.status_code == 200, r.text

    for codigo in _CODIGOS_OBLIGATORIOS:
        p = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/{codigo}",
            headers=headers,
            json={"cumplido_manual": True},
        )
        assert p.status_code == 200, p.text


# ---------------------------------------------------------------------------
# 1. Idempotent success: second radicar on an already-enviada cuenta
# ---------------------------------------------------------------------------


async def test_radicar_segunda_vez_devuelve_mismo_payload_200(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    first = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert first.status_code == 200, first.text

    second = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])

    # Same status code AND identical payload (including fecha_envio) — not just
    # "still succeeds", but proof the second call didn't re-stamp/re-run gates.
    assert second.status_code == 200, second.text
    assert second.json() == first.json()


# ---------------------------------------------------------------------------
# 2. Concurrent double-submit: exactly one transition, checklist gate runs at
#    most once against a state that actually needed it.
# ---------------------------------------------------------------------------


async def test_radicar_concurrente_una_sola_transicion(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent `radicar` calls via `asyncio.gather` must produce exactly one
    ENVIADA transition with one `fecha_envio` — never two distinct stamps, never an
    unhandled exception on the "loser" side.

    HONESTY NOTE: the test DB pool for SQLite (`tests/conftest.py`) keeps a single
    shared in-memory connection so that all sessions see the same schema/data —
    which means two `AsyncSession`s cannot literally hold overlapping open
    transactions on SQLite; the pool checkout serializes them. This test therefore
    proves the code path is race-SAFE (no double transition, no crash) under
    whatever interleaving asyncio/the pool produces here, but it does NOT prove
    true concurrent-transaction serialization — that only happens under Postgres
    (`make test-pg`), where `with_for_update()` actually blocks the second
    transaction's SELECT until the first commits.
    """
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    spy = AsyncMock(side_effect=checklist_service.construir_checklist_completo)
    monkeypatch.setattr(cuenta_cobro_service.checklist_service, "construir_checklist_completo", spy)

    r1, r2 = await asyncio.gather(
        client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"]),
        client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"]),
    )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["estado"] == "enviada"
    assert r2.json()["estado"] == "enviada"
    # Exactly one fecha_envio value must have "won" — both responses converge on it.
    assert r1.json()["fecha_envio"] == r2.json()["fecha_envio"]

    # The checklist gate may run once (the loser short-circuits before reaching it)
    # or, in the rarer interleaving where both readers pass the gate before either
    # writes, twice — but never against a state where a THIRD call would still see
    # BORRADOR (i.e. it must never run against an already-enviada cuenta and still
    # let it through the coherence/checklist path a second material time). What
    # actually matters is asserted above: exactly one estado/fecha_envio survives.
    assert spy.await_count in (1, 2)


# ---------------------------------------------------------------------------
# 2b. Regression (Postgres bug found via `make test-pg`): the pre-transition
#     short-circuit must reload the TRUE current row, not a stale identity-map
#     copy loaded earlier in the same session.
# ---------------------------------------------------------------------------


async def test_radicar_short_circuito_post_lock_refleja_estado_de_otra_sesion(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces, deterministically and without Docker/Postgres, the bug that made
    `test_radicar_concurrente_una_sola_transicion` fail under real Postgres: `db`
    already holds a loaded, non-expired `CuentaCobro` instance (estado=BORRADOR) in
    its identity map — exactly like `radicar_cuenta`'s own top-of-function
    `_get_cuenta_con_ownership` read. A COMPLETELY SEPARATE session then commits the
    ENVIADA transition (standing in for a concurrent winning request under Postgres,
    which uses its own session/identity map). `_leer_estado_bajo_lock` is
    monkeypatched to report ENVIADA — standing in for the `FOR UPDATE` read that,
    under real Postgres, blocks until the other transaction commits and then
    observes its result.

    Before the fix, `radicar_cuenta`'s short-circuit (`estado_bajo_lock == ENVIADA`)
    called `_reload_cuenta_response`, whose plain `select(CuentaCobro)` returned the
    STALE cached instance (still BORRADOR) instead of the row's true current state —
    SQLAlchemy's identity map does not refresh an already-loaded, non-expired object
    from a fresh row unless `populate_existing=True` is set. Fixed by adding
    `populate_existing=True` to `_reload_cuenta_response`'s query.

    Unlike test #7 below (`_construir_y_ganar_la_carrera`), which mutates the row
    through the SAME session via an ORM-enabled bulk UPDATE — whose default
    `synchronize_session='auto'` strategy re-syncs the in-memory object as a side
    effect, masking this exact bug — this test commits the transition through a
    genuinely different session, matching what actually happens across two
    concurrent HTTP requests.
    """
    user = test_user["user"]
    await checklist_service.construir_checklist_completo(db, cuenta)
    for codigo in _CODIGOS_OBLIGATORIOS:
        await checklist_service.marcar_cumplido_manual(db, cuenta.id, codigo)
    await db.commit()
    await db.refresh(cuenta)
    assert cuenta.estado == EstadoCuentaCobro.BORRADOR

    async with async_session_test() as otra_sesion:
        await otra_sesion.execute(
            update(CuentaCobro).where(CuentaCobro.id == cuenta.id).values(estado=EstadoCuentaCobro.ENVIADA)
        )
        await otra_sesion.commit()

    monkeypatch.setattr(
        cuenta_cobro_service, "_leer_estado_bajo_lock", AsyncMock(return_value=EstadoCuentaCobro.ENVIADA)
    )

    resultado = await cuenta_cobro_service.radicar_cuenta(db, user.id, cuenta.id)

    assert resultado.estado == EstadoCuentaCobro.ENVIADA


# ---------------------------------------------------------------------------
# 3. Re-radicar after RECHAZADA stamps a NEW fecha_envio
# ---------------------------------------------------------------------------


async def test_radicar_despues_de_rechazada_nueva_fecha_envio(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, cuenta: CuentaCobro
) -> None:
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    first = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert first.status_code == 200, first.text
    fecha_1 = first.json()["fecha_envio"]

    await db.execute(update(CuentaCobro).where(CuentaCobro.id == cuenta.id).values(estado=EstadoCuentaCobro.RECHAZADA))
    await db.commit()

    second = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert second.status_code == 200, second.text
    assert second.json()["estado"] == "enviada"
    assert second.json()["fecha_envio"] is not None
    assert second.json()["fecha_envio"] != fecha_1


# ---------------------------------------------------------------------------
# 4. aprobada / pagada still reject with 422, unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado_bloqueante", [EstadoCuentaCobro.APROBADA, EstadoCuentaCobro.PAGADA])
async def test_radicar_desde_aprobada_o_pagada_422(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    estado_bloqueante: EstadoCuentaCobro,
) -> None:
    await db.execute(update(CuentaCobro).where(CuentaCobro.id == cuenta.id).values(estado=estado_bloqueante))
    await db.commit()

    resp = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert resp.status_code == 422, resp.text

    result = await db.execute(select(CuentaCobro.estado).where(CuentaCobro.id == cuenta.id))
    assert result.scalar_one() == estado_bloqueante


# ---------------------------------------------------------------------------
# 5. Cross-user cuenta → 404, not a leak
# ---------------------------------------------------------------------------


async def test_radicar_cuenta_de_otro_usuario_404(client: AsyncClient, db: AsyncSession, cuenta: CuentaCobro) -> None:
    from app.core.security import create_access_token, hash_password
    from app.models.usuario import Usuario

    otro = Usuario(
        email="otro-radicar-idemp@example.com",
        nombre="Otro Usuario",
        cedula="987654322",
        password_hash=hash_password("OtroPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro)
    await db.commit()
    await db.refresh(otro)
    token = create_access_token(subject=str(otro.id), role=otro.rol)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# 6. Incomplete checklist raises CHECKLIST_INCOMPLETE BEFORE any write
# ---------------------------------------------------------------------------


async def test_radicar_checklist_incompleto_no_escribe_estado(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, cuenta: CuentaCobro
) -> None:
    resp = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body.get("code") == "CHECKLIST_INCOMPLETE"

    result = await db.execute(select(CuentaCobro.estado, CuentaCobro.fecha_envio).where(CuentaCobro.id == cuenta.id))
    estado, fecha_envio = result.one()
    assert estado == EstadoCuentaCobro.BORRADOR
    assert fecha_envio is None


# ---------------------------------------------------------------------------
# 7. Post-lock short-circuit, triggered via an in-session write: rewinds the
#    checklist-gate call to flip the row underneath us and asserts the
#    resulting response is fresh, not stale.
#
#    RE-DOCUMENTED (round-2 adversarial review, N1): this test's name/docstring
#    used to claim it exercised `cambiar_estado`'s CAS `rowcount == 0` branch.
#    It does NOT: `_construir_y_ganar_la_carrera` mutates the row via an
#    ORM-enabled bulk UPDATE issued on `db` itself (the SAME session `cuenta`
#    is already loaded in). SQLAlchemy's default `synchronize_session='auto'`
#    evaluates the (simple) WHERE clause against in-session objects and
#    updates `cuenta.estado` in-memory too — confirmed by instrumentation:
#    `cuenta.estado` reads back as ENVIADA immediately after. So
#    `_leer_estado_bajo_lock`'s real read also sees ENVIADA, and this test
#    actually exercises `radicar_cuenta`'s PRE-LOCK SHORT-CIRCUIT (the
#    `estado_bajo_lock == EstadoCuentaCobro.ENVIADA` branch just above
#    `cambiar_estado`), never reaching the CAS UPDATE at all. See
#    `test_radicar_cas_loser_verdadero_rowcount_cero` below for a test that
#    actually reaches the CAS `rowcount == 0` branch.
# ---------------------------------------------------------------------------


async def test_radicar_short_circuito_post_lock_via_mutacion_en_sesion(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flips the row to ENVIADA, in the SAME session, right after the checklist
    gate computes its (passing) payload — standing in for a concurrent writer
    that already committed the transition by the time `radicar_cuenta` reaches
    its post-lock check. Asserts the pre-lock short-circuit (see module-level
    re-documentation above) returns the fresh ENVIADA response rather than
    raising or returning stale data.
    """
    user = test_user["user"]

    # Seed + complete the checklist directly through the service so this test
    # doesn't depend on the HTTP client/session split.
    await checklist_service.construir_checklist_completo(db, cuenta)
    for codigo in _CODIGOS_OBLIGATORIOS:
        await checklist_service.marcar_cumplido_manual(db, cuenta.id, codigo)
    await db.commit()
    await db.refresh(cuenta)

    real_construir = checklist_service.construir_checklist_completo

    async def _construir_y_ganar_la_carrera(session: AsyncSession, cta: CuentaCobro, **kwargs: Any) -> dict[str, Any]:
        payload = await real_construir(session, cta, **kwargs)
        # Someone else's transaction already committed the transition to ENVIADA.
        await session.execute(
            update(CuentaCobro).where(CuentaCobro.id == cta.id).values(estado=EstadoCuentaCobro.ENVIADA)
        )
        return payload

    monkeypatch.setattr(
        cuenta_cobro_service.checklist_service,
        "construir_checklist_completo",
        AsyncMock(side_effect=_construir_y_ganar_la_carrera),
    )

    resultado = await cuenta_cobro_service.radicar_cuenta(db, user.id, cuenta.id)

    assert resultado.estado == EstadoCuentaCobro.ENVIADA
    # No exception was raised — the short-circuit path returned the current row.


# ---------------------------------------------------------------------------
# 7b. N1 — the ACTUAL CAS-loser path: `cambiar_estado`'s `rowcount == 0`
#     branch, reached when `_leer_estado_bajo_lock` itself is stale/wrong
#     (e.g. observed via a lock read that doesn't yet reflect another
#     session's commit) but the CAS UPDATE's WHERE still fails to match
#     because the row is genuinely already ENVIADA.
# ---------------------------------------------------------------------------


async def test_radicar_cas_loser_verdadero_rowcount_cero(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces `radicar_cuenta` all the way into `cambiar_estado`'s CAS UPDATE by
    monkeypatching `_leer_estado_bajo_lock` to (incorrectly) report BORRADOR,
    while the row was already moved to ENVIADA by a genuinely SEPARATE session
    (so `db`'s identity map is untouched and still shows BORRADOR too, passing
    `radicar_cuenta`'s top-of-function gate). The CAS UPDATE's WHERE
    (`estado IN (borrador, rechazada)`) then matches zero rows: `cambiar_estado`
    must fall back to re-reading the actual row and return the current (ENVIADA)
    response instead of raising.

    Mutation-kill check: if `cuenta_cobro_service.cambiar_estado`'s
    `if cas_result.rowcount == 0:` branch is poisoned (e.g. inverted or removed),
    the fall-through path happens to return the same (correct) ENVIADA state —
    the row genuinely is ENVIADA in the DB either way, so a plain
    `resultado.estado == ENVIADA` assertion alone would NOT catch it. The spy
    on `_get_cuenta_con_ownership` closes that gap: it is called exactly 3 times
    only when the `rowcount == 0` branch's extra re-read (`actual = await
    _get_cuenta_con_ownership(...)`) actually executes — (1) `radicar_cuenta`'s
    top-of-function read, (2) `cambiar_estado`'s own read, (3) the CAS-loser
    fallback re-read. Poisoning the branch drops this to 2 calls.
    """
    user = test_user["user"]
    await checklist_service.construir_checklist_completo(db, cuenta)
    for codigo in _CODIGOS_OBLIGATORIOS:
        await checklist_service.marcar_cumplido_manual(db, cuenta.id, codigo)
    await db.commit()
    await db.refresh(cuenta)
    assert cuenta.estado == EstadoCuentaCobro.BORRADOR

    async with async_session_test() as otra_sesion:
        await otra_sesion.execute(
            update(CuentaCobro).where(CuentaCobro.id == cuenta.id).values(estado=EstadoCuentaCobro.ENVIADA)
        )
        await otra_sesion.commit()

    monkeypatch.setattr(
        cuenta_cobro_service, "_leer_estado_bajo_lock", AsyncMock(return_value=EstadoCuentaCobro.BORRADOR)
    )
    real_get_ownership = cuenta_cobro_service._get_cuenta_con_ownership
    ownership_spy = AsyncMock(side_effect=real_get_ownership)
    monkeypatch.setattr(cuenta_cobro_service, "_get_cuenta_con_ownership", ownership_spy)

    resultado = await cuenta_cobro_service.radicar_cuenta(db, user.id, cuenta.id)

    assert resultado.estado == EstadoCuentaCobro.ENVIADA
    # No exception was raised — the CAS-loser (`rowcount == 0`) path returned the
    # current row. The 3rd call proves the fallback re-read branch actually ran.
    assert ownership_spy.await_count == 3


# ---------------------------------------------------------------------------
# 7c. N2 — post-lock invalid-transition raise: the row moved to a terminal
#     state (APROBADA/PAGADA) between the top-of-function read and the locked
#     read, and `radicar_cuenta` must still reject it, not silently proceed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado_terminal", [EstadoCuentaCobro.APROBADA, EstadoCuentaCobro.PAGADA])
async def test_radicar_lock_detecta_estado_terminal_post_lock(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
    estado_terminal: EstadoCuentaCobro,
) -> None:
    """`_leer_estado_bajo_lock` is monkeypatched to report a terminal state even
    though the top-of-function read (and this test's own DB row) saw BORRADOR —
    standing in for another transaction moving the row to APROBADA/PAGADA in the
    window between that unlocked top read and the locked pre-transition read.
    `radicar_cuenta` must raise (via `_raise_estado_invalido_para_radicar`), and
    the row must remain unchanged.

    Mutation-kill check: if the post-lock
    `if estado_bajo_lock not in (BORRADOR, RECHAZADA): _raise_estado_invalido_para_radicar(...)`
    branch is removed, this test fails (no exception raised).
    """
    user = test_user["user"]
    await checklist_service.construir_checklist_completo(db, cuenta)
    for codigo in _CODIGOS_OBLIGATORIOS:
        await checklist_service.marcar_cumplido_manual(db, cuenta.id, codigo)
    await db.commit()
    await db.refresh(cuenta)

    monkeypatch.setattr(cuenta_cobro_service, "_leer_estado_bajo_lock", AsyncMock(return_value=estado_terminal))

    with pytest.raises(ValidationError):
        await cuenta_cobro_service.radicar_cuenta(db, user.id, cuenta.id)

    result = await db.execute(select(CuentaCobro.estado).where(CuentaCobro.id == cuenta.id))
    assert result.scalar_one() == EstadoCuentaCobro.BORRADOR


# 8. Query budget guard: see tests/test_query_budgets.py::test_query_budget_radicar
# (kept in that module since it shares its fixtures/QueryCounter — re-measured and
# tightened as part of this slice per the plan's requirement).


# ---------------------------------------------------------------------------
# 9. S1 — the CAS UPDATE's WHERE must exclude soft-deleted rows.
# ---------------------------------------------------------------------------


async def test_radicar_cas_no_transiciona_fila_soft_deleted(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 (adversarial review): `cambiar_estado`'s CAS UPDATE originally matched on
    `id == cuenta_id AND estado IN (borrador, rechazada)` alone, with no
    `deleted_at IS NULL` guard. Simulates a soft-delete landing between the
    ownership read and the CAS UPDATE (e.g. a concurrent hard-delete-adjacent
    request): without the guard, the CAS still matches on `estado` alone and
    silently revives a deleted cuenta into ENVIADA. With the fix, the CAS
    misses (rowcount == 0) and the fallback re-read — which already filters
    `deleted_at IS NULL` — raises NotFoundError instead of transitioning.
    """
    user = test_user["user"]
    cuenta_id = cuenta.id
    real_get = cuenta_cobro_service._get_cuenta_con_ownership

    async def _leer_y_soft_borrar(
        session: AsyncSession, usuario_id: uuid.UUID, cuenta_id_: uuid.UUID, **kwargs: Any
    ) -> CuentaCobro:
        resultado = await real_get(session, usuario_id, cuenta_id_, **kwargs)
        await session.execute(
            update(CuentaCobro).where(CuentaCobro.id == cuenta_id_).values(deleted_at=datetime.now(UTC))
        )
        return resultado

    monkeypatch.setattr(cuenta_cobro_service, "_get_cuenta_con_ownership", _leer_y_soft_borrar)

    with pytest.raises(NotFoundError):
        await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta_id, EstadoCuentaCobro.ENVIADA)

    # Raw check bypassing ownership/deleted_at filters — the row must remain
    # untouched: still BORRADOR, still soft-deleted, no fecha_envio stamped.
    raw = await db.execute(
        select(CuentaCobro.estado, CuentaCobro.deleted_at, CuentaCobro.fecha_envio).where(CuentaCobro.id == cuenta_id)
    )
    estado, deleted_at, fecha_envio = raw.one()
    assert estado == EstadoCuentaCobro.BORRADOR
    assert deleted_at is not None
    assert fecha_envio is None


# ---------------------------------------------------------------------------
# 10. S2 — promote the short-circuit spy probe: gates must NOT re-run on an
#     already-ENVIADA cuenta.
# ---------------------------------------------------------------------------


async def test_radicar_segunda_vez_no_reejecuta_gates(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 (adversarial review): the idempotent short-circuit for an
    already-ENVIADA cuenta was only exercised indirectly (via response equality
    in test #1). This pins it directly: on the second radicar,
    `coherence_validator_service.validar_coherencia` and
    `checklist_service.construir_checklist_completo` must have `await_count == 0`
    — proof the short-circuit returns BEFORE the gates run again, not just that
    the gates happen to produce the same result twice.
    """
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    first = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert first.status_code == 200, first.text

    checklist_spy = AsyncMock(side_effect=checklist_service.construir_checklist_completo)
    monkeypatch.setattr(cuenta_cobro_service.checklist_service, "construir_checklist_completo", checklist_spy)
    coherencia_spy = AsyncMock(side_effect=coherence_validator_service.validar_coherencia)
    monkeypatch.setattr(cuenta_cobro_service.coherence_validator_service, "validar_coherencia", coherencia_spy)

    second = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=test_user["headers"])
    assert second.status_code == 200, second.text

    assert checklist_spy.await_count == 0
    assert coherencia_spy.await_count == 0


# ---------------------------------------------------------------------------
# 11. W1 — mutation-killer for the pre-transition lock (SQLite, no Docker
#     needed): pins `with_for_update()` at the SQL-construction level.
# ---------------------------------------------------------------------------


async def test_radicar_lock_read_contiene_for_update_en_postgres(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """W1 (adversarial review): on SQLite, `for_update=True -> False` on the
    pre-transition lock read survives every other test in this suite (SQLite
    silently no-ops `FOR UPDATE`). This asserts at the SQL-construction level —
    captures every statement `radicar_cuenta` sends through the session and
    compiles it against the postgresql dialect, so removing `with_for_update()`
    fails this test even without Docker/Postgres.
    """
    from sqlalchemy.dialects import postgresql

    user = test_user["user"]
    await checklist_service.construir_checklist_completo(db, cuenta)
    for codigo in _CODIGOS_OBLIGATORIOS:
        await checklist_service.marcar_cumplido_manual(db, cuenta.id, codigo)
    await db.commit()
    await db.refresh(cuenta)

    captured: list[Any] = []
    original_execute = db.execute

    async def _spy_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(stmt)
        return await original_execute(stmt, *args, **kwargs)

    db.execute = _spy_execute  # type: ignore[method-assign]
    try:
        await cuenta_cobro_service.radicar_cuenta(db, user.id, cuenta.id)
    finally:
        db.execute = original_execute  # type: ignore[method-assign]

    locked = [
        stmt
        for stmt in captured
        if hasattr(stmt, "compile") and "FOR UPDATE" in str(stmt.compile(dialect=postgresql.dialect()))
    ]
    assert locked, "expected radicar_cuenta's pre-transition read to use with_for_update()"


# ---------------------------------------------------------------------------
# 12. W1 — Postgres-only: the pre-transition lock actually blocks a concurrent
#     transaction (`make test-pg`; skipped on SQLite).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _IS_PG, reason="row-lock timing is only observable under real Postgres")
async def test_radicar_lock_bloquea_segunda_transaccion_bajo_postgres(cuenta: CuentaCobro) -> None:
    """W1 (adversarial review): proves `with_for_update()` on the pre-transition
    read genuinely serializes two concurrent transactions under Postgres — the
    SQLite test DB shares a single in-memory connection across sessions, so it
    cannot host two overlapping transactions (see the honesty note on
    `test_radicar_concurrente_una_sola_transicion` above).

    Probes the lock with `nowait=True` instead of racing `asyncio.wait_for`
    against an in-flight `SELECT ... FOR UPDATE`: cancelling an in-flight asyncpg
    statement leaves the connection in an invalid-transaction state
    (`PendingRollbackError` on next use) because the cancellation races the
    server's response, not the query itself — asyncpg doesn't cleanly abort
    mid-flight statements the way a client-side timeout implies. `nowait=True`
    instead asks Postgres to raise IMMEDIATELY (`55P03 lock_not_available`,
    surfaced by SQLAlchemy as a `DBAPIError` wrapping asyncpg's
    `LockNotAvailableError`) if the row is already locked,
    which is both deterministic and leaves the session in a clean,
    rollback-able state.

    Every branch releases both sessions in `finally` so a failing assertion can
    never hang the suite (a session left mid-transaction would otherwise hold
    its lock/connection until GC, potentially blocking later tests).
    """
    session_a = async_session_test()
    session_b = async_session_test()
    try:
        # Session A takes the lock and holds its transaction open.
        await cuenta_cobro_service._leer_estado_bajo_lock(session_a, cuenta.id)

        # Session B's own locking read of the SAME row must fail IMMEDIATELY
        # (not block) while A still holds its transaction.
        with pytest.raises(DBAPIError) as exc_info:
            await cuenta_cobro_service._leer_estado_bajo_lock(session_b, cuenta.id, nowait=True)
        # 55P03 = lock_not_available — confirms this is genuinely the row-lock
        # contention we intend to probe, not some unrelated DBAPIError.
        assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"
        # The failed statement left B's transaction unusable — roll back before
        # B's session can be reused for the follow-up locked read below.
        await session_b.rollback()

        await session_a.commit()

        # Now that A released the lock, B's normal (blocking) read completes
        # promptly and observes the post-release state.
        estado = await asyncio.wait_for(
            cuenta_cobro_service._leer_estado_bajo_lock(session_b, cuenta.id),
            timeout=2.0,
        )
        assert estado == EstadoCuentaCobro.BORRADOR
        await session_b.commit()
    finally:
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()
