"""Regression tests for the redundant `_reload_contrato_response` /
`_reload_cuenta_response` removals (radicacion-sin-friccion, slice 2.2).

Each site removed a re-SELECT that duplicated data the caller's in-session
object already had loaded and accurate. These tests pin BOTH properties for
every touched site:

1. Query count is measurably lower than before the fix (regression guard
   against someone reintroducing the redundant reload).
2. The response's field values are byte-identical to what a full reload would
   have produced — not just "the endpoint didn't crash". This is the part that
   would catch a genuinely-needed-eager-load site being wrongly stripped.

Sites that legitimately keep a reload (verified by the audit, NOT touched
here): `contrato_service.crear_contrato` (sibling `Obligacion` rows inserted
via a bare `contrato_id=` FK are never synced into `contrato.obligaciones` in
memory), `cuenta_cobro_service.cambiar_estado`'s ENVIADA-CAS-winner branch
(the `cuenta` object is explicitly `db.expire()`-d right before), and
`radicar_cuenta`'s post-lock short-circuit (a concurrent transaction may have
committed a change this session's in-memory `cuenta` doesn't know about).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.secop import SecopContrato
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import QueryCounter

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-RELOAD-001",
        objeto="Servicios profesionales para pruebas de redundancia de recargas",
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
        mes=2,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        numero_cuota=1,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── contrato_service: obtener_contrato / actualizar_contrato ────────────────


async def test_actualizar_contrato_query_count_lower_than_pre_fix_baseline(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, query_counter: QueryCounter
) -> None:
    """PATCH /api/v1/contratos/{id} used to re-SELECT the contrato (+ its
    `.obligaciones`, which cascades into `Obligacion.actividades`) purely to
    rebuild the response, even though the in-session `contrato` object was
    already accurate after the `setattr` + `flush()` above it. Pre-fix this
    endpoint issued strictly more queries than the tightened ceiling below —
    this budget will fail loudly if the redundant reload is reintroduced."""
    headers = test_user["headers"]
    query_counter.reset()

    resp = await client.patch(
        f"/api/v1/contratos/{contrato.id}",
        headers=headers,
        json={"supervisor_nombre": "Nuevo Supervisor"},
    )

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(8, label="PATCH /api/v1/contratos/{id}")


async def test_actualizar_contrato_valor_fields_match_db_normalized_scale(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Field-scale edge case: PATCHing a monetary field with a value that has
    fewer decimal digits than the column's NUMERIC(15, 2) scale (e.g. one
    decimal digit) must come back DB-normalized (two decimal digits), not the
    raw un-normalized value the caller sent. Postgres normalizes scale only on
    a round-trip through the DB (INSERT/UPDATE + re-read) — the in-session
    Python object never sees that normalization on its own, so the response
    must be built from a value that was actually re-fetched from the DB."""
    headers = test_user["headers"]

    resp = await client.patch(
        f"/api/v1/contratos/{contrato.id}",
        headers=headers,
        json={"valor_total": "1000000.5", "valor_adicion": "500000.1", "valor_mensual": "1234567.8"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valor_total"] == "1000000.50"
    assert body["valor_adicion"] == "500000.10"
    assert body["valor_mensual"] == "1234567.80"

    # `populate_existing()` forces this query to overwrite the fixture object's
    # already-identity-mapped attributes with what's actually in the DB right
    # now — a plain `select()` would silently return the stale pre-PATCH values
    # cached from the fixture's own setup, proving nothing about persistence.
    row = (
        await db.execute(select(Contrato).where(Contrato.id == contrato.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert str(row.valor_total) == "1000000.50"
    assert str(row.valor_adicion) == "500000.10"
    assert str(row.valor_mensual) == "1234567.80"


async def test_actualizar_contrato_preserves_secop_enrichment(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Field-value edge case: `url_proceso` is populated ONLY via the SECOP
    enrichment query inside `_response_from_contrato`, which the refactor
    deliberately KEPT. If the refactor had accidentally dropped that lookup
    (e.g. by returning `ContratoResponse.model_validate(contrato)` directly
    without going through `_response_from_contrato`), this would silently
    regress to `url_proceso=None` — a plain 200-OK / no-crash test would not
    catch it."""
    secop = SecopContrato(
        id_contrato_secop="SECOP-RELOAD-001",
        cedula_contratista="123456789",
        numero_contrato=contrato.numero_contrato,
        datos_raw={"urlproceso": {"url": "https://www.secop.gov.co/proceso/abc123"}},
    )
    db.add(secop)
    await db.commit()

    headers = test_user["headers"]
    resp = await client.patch(
        f"/api/v1/contratos/{contrato.id}",
        headers=headers,
        json={"cargo_supervisor": "Director"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cargo_supervisor"] == "Director"
    assert body["url_proceso"] == "https://www.secop.gov.co/proceso/abc123"


# ── cuenta_cobro_service: crear_cuenta_cobro ─────────────────────────────────


async def test_crear_cuenta_cobro_query_count_lower_than_pre_fix_baseline(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, query_counter: QueryCounter
) -> None:
    """POST /api/v1/cuentas-cobro/ used to re-SELECT the just-created cuenta
    (with actividades/contrato eager-loaded) even though it is a brand-new,
    already fully in-memory object with an empty `.actividades` collection."""
    headers = test_user["headers"]
    query_counter.reset()

    resp = await client.post(
        "/api/v1/cuentas-cobro/",
        headers=headers,
        json={"contrato_id": str(contrato.id), "mes": 6, "anio": 2024},
    )

    assert resp.status_code == 201, resp.text
    query_counter.assert_budget(15, label="POST /api/v1/cuentas-cobro/")


async def test_crear_cuenta_cobro_response_fields_match_persisted_row(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Field-value edge case: the response built directly from the in-memory
    object must match what is ACTUALLY persisted — not stale defaults — proving
    the in-session object truly had fresh, complete data at response time."""
    headers = test_user["headers"]

    resp = await client.post(
        "/api/v1/cuentas-cobro/",
        headers=headers,
        json={"contrato_id": str(contrato.id), "mes": 7, "anio": 2024, "valor": "1500000.00"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mes"] == 7
    assert body["anio"] == 2024
    assert float(body["valor"]) == 1_500_000.0
    assert body["estado"] == "borrador"
    assert body["actividades"] == []

    row = (await db.execute(select(CuentaCobro).where(CuentaCobro.id == uuid.UUID(body["id"])))).scalar_one()
    assert row.mes == 7
    assert row.anio == 2024
    assert float(row.valor) == 1_500_000.0


async def test_crear_cuenta_cobro_valor_matches_db_normalized_scale(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Field-scale edge case: POSTing `valor` with one decimal digit must come
    back DB-normalized to the column's NUMERIC(15, 2) scale (two decimal
    digits), not the raw un-normalized value. Reproduces the reviewer's exact
    probe: a fresh independent re-query of the same row must match the
    response body exactly."""
    headers = test_user["headers"]

    resp = await client.post(
        "/api/v1/cuentas-cobro/",
        headers=headers,
        json={"contrato_id": str(contrato.id), "mes": 9, "anio": 2024, "valor": "1234567.8"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["valor"] == "1234567.80"

    row = (await db.execute(select(CuentaCobro).where(CuentaCobro.id == uuid.UUID(body["id"])))).scalar_one()
    assert str(row.valor) == "1234567.80"
    assert str(row.valor) == body["valor"]


# ── cuenta_cobro_service: actualizar_cuenta_cobro ────────────────────────────


async def test_actualizar_cuenta_cobro_query_count_lower_than_pre_fix_baseline(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro, query_counter: QueryCounter
) -> None:
    headers = test_user["headers"]
    query_counter.reset()

    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}",
        headers=headers,
        json={"valor": "2000000.00"},
    )

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(9, label="PATCH /api/v1/cuentas-cobro/{id}")


async def test_actualizar_cuenta_cobro_response_reflects_update(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    headers = test_user["headers"]

    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}",
        headers=headers,
        json={"mes": 8, "anio": 2025, "contexto_usuario": "Nuevo contexto del mes"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mes"] == 8
    assert body["anio"] == 2025
    assert body["contexto_usuario"] == "Nuevo contexto del mes"


async def test_actualizar_cuenta_cobro_valor_matches_db_normalized_scale(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """Field-scale edge case: PATCHing `valor` with one decimal digit must come
    back DB-normalized to the column's NUMERIC(15, 2) scale (two decimal
    digits), reproducing the reviewer's exact probe against the update path."""
    headers = test_user["headers"]

    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}",
        headers=headers,
        json={"valor": "1234567.8"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valor"] == "1234567.80"

    # `populate_existing()` forces this query to overwrite the fixture object's
    # already-identity-mapped attributes with what's actually in the DB right
    # now — a plain `select()` would silently return the stale pre-PATCH value
    # cached from the fixture's own setup, proving nothing about persistence.
    row = (
        await db.execute(
            select(CuentaCobro).where(CuentaCobro.id == cuenta.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert str(row.valor) == "1234567.80"
    assert str(row.valor) == body["valor"]


# ── cuenta_cobro_service: cambiar_estado generic (non-ENVIADA) branch ────────


async def test_cambiar_estado_reopen_query_count_lower_than_pre_fix_baseline(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, query_counter: QueryCounter
) -> None:
    """PATCH .../estado to BORRADOR (reopen) exercises `cambiar_estado`'s generic
    (non-ENVIADA) branch — the redundant reload removed there."""
    cuenta.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    headers = test_user["headers"]
    query_counter.reset()

    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/estado",
        headers=headers,
        json={"estado": "borrador"},
    )

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(9, label="PATCH /api/v1/cuentas-cobro/{id}/estado (reopen)")


async def test_cambiar_estado_reopen_clears_envio_fields(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """Field-value edge case: reopening must clear `fecha_envio`/`pdf_storage_key`
    (`cambiar_estado`'s documented BORRADOR-reopen contract) — proving the
    directly-built response reflects the mutation just made in-memory, not a
    stale pre-mutation snapshot."""
    from datetime import UTC, datetime

    cuenta.estado = EstadoCuentaCobro.ENVIADA
    cuenta.fecha_envio = datetime.now(UTC)
    cuenta.pdf_storage_key = "pdfs/old/stale.pdf"
    await db.commit()

    headers = test_user["headers"]
    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/estado",
        headers=headers,
        json={"estado": "borrador"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estado"] == "borrador"
    assert body["fecha_envio"] is None
    assert body["pdf_storage_key"] is None
