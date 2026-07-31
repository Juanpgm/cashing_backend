"""Integration tests (httpx.AsyncClient) for the background classification and
reclassification endpoints — evidence-classification-jobs + evidence-
reclassification capabilities. Contract-shape tests: the exact payload keys
these assert are the cross-repo contract the frontend batch consumes."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.core.security import hash_password
from app.models.actividad import Actividad
from app.models.clasificacion_job import ClasificacionEvidenciasJob
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CLAS-API-001",
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
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes tecnicos mensuales",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


@pytest.fixture
async def actividad_stub(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(cuenta_cobro_id=cuenta_cobro.id, descripcion="Evidencias sin clasificar")
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@pytest.fixture
async def evidencia(db: AsyncSession, actividad_stub: Actividad) -> Evidencia:
    ev = Evidencia(
        actividad_id=actividad_stub.id,
        storage_key="evidencias/test/informe.txt",
        nombre_archivo="informe.txt",
        tipo_archivo="text/plain",
        tamano_bytes=10,
        texto_extraido="texto",
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return ev


# ── GET .../clasificacion — per-file/per-obligación progress contract ───────


async def test_get_clasificacion_status_contract_shape_partial_progress(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
    evidencia: Evidencia,
    actividad_stub: Actividad,
) -> None:
    """A known partial-progress DB state (one classified evidencia, job status
    `running`, one still-pending evidencia) must match the documented contract
    shape exactly (evidence-classification-jobs: TDD/integration coverage for
    job and status endpoints)."""
    db.add(EvidenciaObligacion(evidencia_id=evidencia.id, obligacion_id=obligacion.id, confianza="alta"))
    ev2 = Evidencia(
        actividad_id=actividad_stub.id,
        storage_key="evidencias/test/otro.txt",
        nombre_archivo="otro.txt",
        tipo_archivo="text/plain",
        tamano_bytes=5,
        texto_extraido="otro texto",
    )
    db.add(ev2)
    db.add(ClasificacionEvidenciasJob(cuenta_cobro_id=cuenta_cobro.id, status="running", total=2, procesadas=1))
    await db.commit()
    await db.refresh(ev2)

    resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/clasificacion", headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["cuenta_cobro_id"] == str(cuenta_cobro.id)
    assert data["status"] == "running"
    assert data["total"] == 2
    assert data["procesadas"] == 1
    assert data["error"] is None
    assert len(data["evidencias"]) == 2

    por_id = {item["id"]: item for item in data["evidencias"]}
    clasificada = por_id[str(evidencia.id)]
    assert clasificada["estado"] == "clasificado"
    assert clasificada["obligaciones"][0]["obligacion_id"] == str(obligacion.id)
    assert clasificada["obligaciones"][0]["confianza"] == "alta"

    pendiente = por_id[str(ev2.id)]
    assert pendiente["estado"] == "pendiente"
    assert pendiente["obligaciones"] == []


async def test_get_clasificacion_sin_autenticacion(client: AsyncClient, cuenta_cobro: CuentaCobro) -> None:
    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/clasificacion")
    assert resp.status_code == 401


# ── POST .../clasificar — (re)trigger the background job ────────────────────


async def test_post_clasificar_returns_202_and_job_shape(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, evidencia: Evidencia
) -> None:
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/clasificar", headers=test_user["headers"]
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["cuenta_cobro_id"] == str(cuenta_cobro.id)
    assert data["status"] in {"pending", "running", "done"}
    assert data["total"] == 1


@pytest.fixture
async def cuenta_ajena(db: AsyncSession) -> CuentaCobro:
    """A cuenta belonging to a DIFFERENT user — verifies the classification
    trigger endpoint enforces ownership like its sibling endpoints (RISK-001:
    no cross-tenant job triggering / progress read)."""
    otro_user = Usuario(
        email="otro-usuario-clas@example.com",
        nombre="Otro Usuario",
        cedula="987654322",
        telefono="+573009876544",
        password_hash=hash_password("OtherPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro_user)
    await db.commit()
    await db.refresh(otro_user)

    otro_contrato = Contrato(
        usuario_id=otro_user.id,
        numero_contrato="CTR-CLAS-AJENO-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(otro_contrato)
    await db.commit()
    await db.refresh(otro_contrato)

    otra_cuenta = CuentaCobro(
        contrato_id=otro_contrato.id, mes=5, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1
    )
    db.add(otra_cuenta)
    await db.commit()
    await db.refresh(otra_cuenta)
    return otra_cuenta


async def test_post_clasificar_otro_usuario_da_404_y_no_crea_job(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta_ajena: CuentaCobro
) -> None:
    """User A must not be able to trigger classification on user B's cuenta,
    nor read its total/procesadas counts (RISK-001). Matches the sibling
    ownership-check endpoints (e.g. `test_subir_evidencias_cuenta_otro_usuario_403`):
    `_get_cuenta_con_ownership` raises `ForbiddenError` → 403 for a wrong owner."""
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_ajena.id}/evidencias/clasificar", headers=test_user["headers"]
    )
    assert resp.status_code == 403

    rows = (
        (
            await db.execute(
                select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_ajena.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ── PATCH .../{evidencia_id}/obligaciones — reclassification wiring ─────────


async def test_patch_obligaciones_add_then_remove_via_endpoint(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    evidencia: Evidencia,
    obligacion: Obligacion,
) -> None:
    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/{evidencia.id}/obligaciones",
        json={"add": [str(obligacion.id)], "remove": [], "confirm": [], "no_aplica": False},
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evidencia_id"] == str(evidencia.id)
    assert len(data["obligaciones"]) == 1
    assert data["obligaciones"][0]["obligacion_id"] == str(obligacion.id)
    assert data["obligaciones"][0]["status"] == "confirmed"
    assert data["obligaciones"][0]["source"] == "user"

    resp2 = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/{evidencia.id}/obligaciones",
        json={"add": [], "remove": [str(obligacion.id)], "confirm": [], "no_aplica": False},
        headers=test_user["headers"],
    )
    assert resp2.status_code == 200
    assert resp2.json()["obligaciones"] == []


async def test_patch_obligaciones_evidencia_fuera_de_cuenta_404(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    resp = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/{uuid.uuid4()}/obligaciones",
        json={"add": [], "remove": [], "confirm": [], "no_aplica": False},
        headers=test_user["headers"],
    )
    assert resp.status_code == 404
