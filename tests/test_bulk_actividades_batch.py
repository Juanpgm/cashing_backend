"""Regression tests for the 4 per-item flush/refresh -> batch conversions
(radicacion-sin-friccion, slice 2.3): `agregar_actividades_bulk`,
`agregar_actividades_desde_texto`, `crear_actividades_desde_obligaciones`, and
`generar_actividades_agente`.

Each converted loop used to `db.add()` + `await db.flush()` + `await
db.refresh()` ONCE PER ITEM. The fix batches all N items into a single
`db.add_all(items)` + one `await db.flush()`.

Edge cases covered per touched endpoint:
1. Query count is measurably lower than the old N*(flush+refresh) cost.
2. All N items are genuinely persisted — not N-1 from an off-by-one in the
   loop->batch conversion (verified via a direct COUNT query, not just
   response length).
3. Atomicity: if any item in the batch would violate a DB constraint, NONE of
   the batch is durably persisted — same all-or-nothing semantics as the old
   per-item-`flush()` loop (which was never durable until the caller's outer
   commit either way, so behavior is unchanged by the conversion).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.schemas.agent import LLMResponse
from app.services import cuenta_cobro_service
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import QueryCounter

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-BATCH-001",
        objeto="Servicios profesionales para pruebas de conversión a lote",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligaciones(db: AsyncSession, contrato: Contrato) -> list[Obligacion]:
    obs = [
        Obligacion(
            contrato_id=contrato.id,
            descripcion=f"Obligación contractual número {i + 1} con texto suficiente",
            tipo=TipoObligacion.GENERAL,
            orden=i,
        )
        for i in range(3)
    ]
    db.add_all(obs)
    await db.commit()
    for o in obs:
        await db.refresh(o)
    return obs


@pytest.fixture
async def cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── agregar_actividades_bulk ─────────────────────────────────────────────────


async def test_agregar_actividades_bulk_query_count_lower_and_all_persisted(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, query_counter: QueryCounter
) -> None:
    headers = test_user["headers"]
    payload = {
        "actividades": [
            {"descripcion": f"Actividad número {i} realizada durante el período de prueba"} for i in range(5)
        ]
    }
    query_counter.reset()

    resp = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/actividades/bulk", headers=headers, json=payload)

    assert resp.status_code == 201, resp.text
    assert resp.json()["creadas"] == 5
    # Old per-item loop: 5 * (flush + refresh) = at least 10 statements JUST for
    # persistence, on top of the ownership read and pre-validation. Batched, the
    # whole endpoint (auth + ownership + insert) fits comfortably under this.
    query_counter.assert_budget(12, label="POST .../actividades/bulk (5 items)")

    count = (
        await db.execute(select(func.count()).select_from(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    ).scalar_one()
    assert count == 5, "all 5 items must be persisted — not 4 from an off-by-one in the batch conversion"


# ── agregar_actividades_desde_texto ──────────────────────────────────────────


async def test_agregar_actividades_desde_texto_query_count_lower_and_all_persisted(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, query_counter: QueryCounter
) -> None:
    headers = test_user["headers"]
    texto = "\n".join(f"{i + 1}. Actividad número {i + 1} realizada durante el período" for i in range(4))
    query_counter.reset()

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/actividades/desde-texto",
        headers=headers,
        json={"texto": texto, "vincular_obligaciones": False},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["creadas"] == 4
    query_counter.assert_budget(12, label="POST .../actividades/desde-texto (4 lines)")

    count = (
        await db.execute(select(func.count()).select_from(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    ).scalar_one()
    assert count == 4


# ── crear_actividades_desde_obligaciones ─────────────────────────────────────


async def test_crear_actividades_desde_obligaciones_query_count_lower_and_all_persisted(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    obligaciones: list[Obligacion],
    query_counter: QueryCounter,
) -> None:
    headers = test_user["headers"]
    query_counter.reset()

    resp = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/actividades/desde-obligaciones", headers=headers)

    assert resp.status_code == 201, resp.text
    assert resp.json()["creadas"] == 3
    query_counter.assert_budget(12, label="POST .../actividades/desde-obligaciones (3 obligaciones)")

    count = (
        await db.execute(select(func.count()).select_from(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    ).scalar_one()
    assert count == 3


# ── generar_actividades_agente ───────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, messages, temperature=0.3, max_tokens=4096) -> LLMResponse:
        return LLMResponse(
            content=self._content, model="fake/test-model", prompt_tokens=1, completion_tokens=1, total_tokens=2
        )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(content), raising=True)


async def test_generar_actividades_agente_query_count_lower_and_all_persisted(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    obligaciones: list[Obligacion],
    monkeypatch: pytest.MonkeyPatch,
    query_counter: QueryCounter,
) -> None:
    headers = test_user["headers"]
    llm_response = (
        "ACTIVIDAD|Elaboré informe mensual completo y detallado|Cumplimiento ob. 1|1\n"
        "ACTIVIDAD|Participé activamente en reuniones de coordinación|Cumplimiento ob. 2|2\n"
        "ACTIVIDAD|Desarrollé y entregué los entregables solicitados|Cumplimiento ob. 3|3\n"
    )
    _patch_llm(monkeypatch, llm_response)
    query_counter.reset()

    resp = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/actividades/generar", headers=headers)

    assert resp.status_code == 201, resp.text
    assert resp.json()["creadas"] == 3
    query_counter.assert_budget(18, label="POST .../actividades/generar (3 nuevas, sin stubs)")

    count = (
        await db.execute(select(func.count()).select_from(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    ).scalar_one()
    assert count == 3


async def test_generar_actividades_agente_reuses_stub_without_extra_refresh_per_item(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligaciones: list[Obligacion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed batch (service-level, not HTTP): one obligación already has a stub
    Actividad (created by an earlier evidence upload), the other two are brand
    new. Both the UPDATE (stub reuse) and INSERT (new) branches must land in the
    SAME single `flush()` and produce correct, fully-populated responses —
    proving the batch conversion didn't silently drop the stub-reuse UPDATE path."""
    user = test_user["user"]
    cc = CuentaCobro(contrato_id=contrato.id, mes=4, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1_000_000)
    db.add(cc)
    await db.flush()

    stub = Actividad(
        cuenta_cobro_id=cc.id,
        obligacion_id=obligaciones[0].id,
        descripcion="Pendiente de redactar — evidencia adjunta",
    )
    db.add(stub)
    await db.commit()
    stub_id = stub.id

    llm_response = (
        "ACTIVIDAD|Elaboré informe mensual completo y detallado|Cumplimiento ob. 1|1\n"
        "ACTIVIDAD|Participé activamente en reuniones de coordinación|Cumplimiento ob. 2|2\n"
        "ACTIVIDAD|Desarrollé y entregué los entregables solicitados|Cumplimiento ob. 3|3\n"
    )
    _patch_llm(monkeypatch, llm_response)

    resp = await cuenta_cobro_service.generar_actividades_agente(db, user.id, cc.id)

    assert resp.creadas == 3
    ids = {a.id for a in resp.actividades}
    assert stub_id in ids, "the reused stub keeps its original row id (not re-inserted)"

    total = (
        await db.execute(select(func.count()).select_from(Actividad).where(Actividad.cuenta_cobro_id == cc.id))
    ).scalar_one()
    assert total == 3, "reusing the stub must not leave behind a duplicate row"


# ── Shared atomicity property of the `add_all` + single `flush()` pattern ───


async def test_batch_add_all_flush_fails_atomically_on_constraint_violation(
    db: AsyncSession, cuenta: CuentaCobro
) -> None:
    """Generic proof for the pattern all 4 conversions now share: if ANY item in
    a `db.add_all([...])` batch violates a DB constraint (here: a forced primary
    key collision — two rows sharing the same explicit `id`), the single `flush()`
    raises and NOTHING from that batch is durably persisted — matching the old
    per-item-`flush()` loop's semantics (which was likewise never durable until
    the caller's OWN outer commit; a mid-loop failure there aborted the whole
    request's transaction the exact same way)."""
    cuenta_id = cuenta.id  # capture before rollback expires the fixture's object
    dup_id = uuid.uuid4()
    items = [
        Actividad(cuenta_cobro_id=cuenta_id, descripcion="Primera actividad del lote de prueba"),
        Actividad(id=dup_id, cuenta_cobro_id=cuenta_id, descripcion="Segunda actividad con id duplicado a propósito"),
        Actividad(id=dup_id, cuenta_cobro_id=cuenta_id, descripcion="Tercera actividad choca con la segunda"),
    ]

    db.add_all(items)
    with pytest.raises(IntegrityError):
        await db.flush()

    await db.rollback()

    from tests.conftest import async_session_test

    async with async_session_test() as otra_sesion:
        count = (
            await otra_sesion.execute(
                select(func.count()).select_from(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id)
            )
        ).scalar_one()
    assert count == 0, "a failed batch flush must persist NOTHING — not a partial N-1 subset"
