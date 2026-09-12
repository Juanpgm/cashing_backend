"""Query-count regression budgets for the highest-traffic endpoints
(radicacion-sin-friccion, slice 0.2).

These are RATCHETS pinned to the CURRENT measured baseline (see the comment
above each `assert_budget` call for the exact number and how it was measured)
— NOT aspirational targets. They must pass today and fail loudly the moment
someone adds a query (e.g. an N+1 introduced by a missing `selectin`/`joined`
load). If a future change genuinely needs more queries, re-measure and bump
the budget deliberately in the same PR, with a comment explaining why.

Fixtures build a realistic, non-degenerate scenario per the audit: 1 contrato,
1 obligación, 1 cuenta_cobro with a fully-completed checklist (RPC satisfied by
a real uploaded `DocumentoFuente`, the remaining mandatory requisitos marked
cumplido_manual — a mixed shape, since a real production cuenta always has at
least one uploaded document and `construir_checklist_completo` only issues its
`documento_fuente` selectin lookup when at least one row has a non-NULL FK), 1
actividad, 1 evidencia.

Skipped on Postgres (`TEST_DATABASE_URL` set): these counts were measured on
SQLite and are not guaranteed byte-for-byte on Postgres (different pooling /
statement chatter, see `QueryCounter`'s docstring in conftest.py) — asserting
them there would produce a misleading "budget exceeded" failure instead of an
honest skip.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _IS_PG, QueryCounter

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        _IS_PG,
        reason="query-count budgets measured on SQLite only, see module docstring",
    ),
]

# A minimal valid PDF header — enough for the upload endpoint's magic-byte
# MIME sniff (core/file_validation.py), same fixture content style as
# test_checklist_api.py's RPC upload test.
_PDF_MAGIC = b"%PDF-1.4 sample pdf content here"

# Mandatory (obligatorio=True) codes in the standard catalog seed — same list
# `test_radicar.py` uses to satisfy `computar_resumen`'s radicacion_lista gate.
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

# RPC is satisfied by a real upload (below) instead of cumplido_manual, so the
# fixture exercises the `documento_fuente` selectin lookup that a checklist
# with zero uploaded documents never triggers.
_CODIGOS_MANUALES = [c for c in _CODIGOS_OBLIGATORIOS if c != "RPC"]


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-QUERY-BUDGET-001",
        objeto="Servicios profesionales para pruebas de presupuesto de queries",
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


@pytest.fixture
async def cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    """Cuenta in BORRADOR with the checklist gate already resolved (estandar)."""
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        numero_cuota=1,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def actividad_con_evidencia(db: AsyncSession, cuenta: CuentaCobro, obligacion: Obligacion) -> Actividad:
    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada durante el periodo",
        justificacion="Justificación detallada de la actividad realizada",
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
    await db.refresh(act)
    await db.refresh(cuenta)
    return act


async def _completar_checklist(client: AsyncClient, headers: dict[str, str], cuenta_id: uuid.UUID) -> None:
    """Seed the checklist rows, satisfy RPC with a real uploaded document, and
    mark every other mandatory requisito as cumplido_manual — a mixed shape
    matching real production data (see module docstring), not the all-manual
    pattern `test_radicar.py`'s helper of the same name uses."""
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist", headers=headers)
    assert r.status_code == 200, r.text

    up = await client.post(
        "/api/v1/documentos/upload",
        headers=headers,
        params={"tipo": "rpc", "cuenta_cobro_id": str(cuenta_id), "requisito_codigo": "RPC"},
        files={"file": ("rpc.pdf", _PDF_MAGIC, "application/pdf")},
    )
    assert up.status_code == 201, up.text

    for codigo in _CODIGOS_MANUALES:
        p = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/{codigo}",
            headers=headers,
            json={"cumplido_manual": True},
        )
        assert p.status_code == 200, p.text


@pytest.fixture
async def escenario_completo(
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    cuenta: CuentaCobro,
    actividad_con_evidencia: Actividad,
) -> dict[str, Any]:
    """Full realistic scenario with the checklist fully completed — required
    for the radicar budget (radicar 400s on an incomplete checklist) and
    reused by every other endpoint here so all budgets measure against the
    same non-degenerate, near-production-shaped data."""
    await _completar_checklist(client, test_user["headers"], cuenta.id)
    return {
        "contrato": contrato,
        "cuenta": cuenta,
        "actividad": actividad_con_evidencia,
        "headers": test_user["headers"],
    }


# ── Endpoint budgets ─────────────────────────────────────────────────────────
#
# Each budget below was measured by first running this exact test with
# `assert_budget(1, ...)` (an intentionally impossible ceiling), reading the
# actual count off the failure message's statement dump, then pinning the
# budget to that exact number — on SQLite (the default TEST_DATABASE_URL),
# 2026-09-12. These are the REAL measured counts, not audit estimates — audit
# estimates are noted in each docstring for comparison only.


async def test_query_budget_list_contratos(
    client: AsyncClient, escenario_completo: dict[str, Any], query_counter: QueryCounter
) -> None:
    """GET /api/v1/contratos/ — audit estimate ~11. Measured baseline: 11."""
    headers = escenario_completo["headers"]
    query_counter.reset()

    resp = await client.get("/api/v1/contratos/", headers=headers)

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(11, label="GET /api/v1/contratos/")


async def test_query_budget_detail_contrato(
    client: AsyncClient, escenario_completo: dict[str, Any], query_counter: QueryCounter
) -> None:
    """GET /api/v1/contratos/{id} — audit estimate ~19. Measured baseline: 19."""
    contrato = escenario_completo["contrato"]
    headers = escenario_completo["headers"]
    query_counter.reset()

    resp = await client.get(f"/api/v1/contratos/{contrato.id}", headers=headers)

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(19, label="GET /api/v1/contratos/{id}")


async def test_query_budget_checklist(
    client: AsyncClient, escenario_completo: dict[str, Any], query_counter: QueryCounter
) -> None:
    """GET /api/v1/cuentas-cobro/{id}/checklist — audit estimate ~36-40.
    Measured baseline: 47 (above the estimate's upper bound — the fixture's
    fully-completed checklist plus a real actividad+evidencia+obligacion graph,
    AND a real uploaded RPC document (via the actual `/documentos/upload`
    endpoint, not a raw ORM insert), exercises more of
    `construir_checklist_completo` than the audit's assumed scenario: the
    `evidencia_obligacion` join, PLUS the `documento_fuente` selectin lookup
    and its own candidate/confianza resolution, which only fire once at least
    one requisito row has a real linked document. A checklist with zero
    uploaded documents (every requisito cumplido_manual, no real upload)
    measures 44 instead — 47 is pinned here because that mixed shape is what
    real production data actually looks like. Re-measured directly against
    this test (not extrapolated) after a prior estimate of 45 turned out
    wrong by 2 — see the adversarial review this fixture went through."""
    cuenta = escenario_completo["cuenta"]
    headers = escenario_completo["headers"]
    query_counter.reset()

    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=headers)

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(47, label="GET /cuentas-cobro/{id}/checklist")


async def test_query_budget_radicar(
    client: AsyncClient, escenario_completo: dict[str, Any], query_counter: QueryCounter
) -> None:
    """POST /api/v1/cuentas-cobro/{id}/radicar — audit estimate ~60-80.
    Measured baseline: 80 (radicar rebuilds the full checklist to re-validate
    the radicacion_lista gate before flipping estado, so it pays the same cost
    as the checklist GET above, 47, plus the state-machine transition)."""
    cuenta = escenario_completo["cuenta"]
    headers = escenario_completo["headers"]
    query_counter.reset()

    resp = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/radicar", headers=headers)

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(80, label="POST /cuentas-cobro/{id}/radicar")


async def test_query_budget_stepper_state(
    client: AsyncClient, escenario_completo: dict[str, Any], query_counter: QueryCounter
) -> None:
    """GET /api/v1/cuentas-cobro/{id}/stepper-state — audit estimate ~25.
    Measured baseline: 24."""
    cuenta = escenario_completo["cuenta"]
    headers = escenario_completo["headers"]
    query_counter.reset()

    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/stepper-state", headers=headers)

    assert resp.status_code == 200, resp.text
    query_counter.assert_budget(24, label="GET /cuentas-cobro/{id}/stepper-state")
