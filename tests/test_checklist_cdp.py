"""Tests for the CDP standard catalog code + lazy link-only alias detection.

See openspec/changes/radicacion-stepper/design.md section 3. CDP ships as an
additive, non-blocking (`obligatorio: False`) nivel-contrato catalog code.
Backfill is LAZY and LINK-ONLY: no existing document or link is ever moved or
deleted, only additional links are created at `asegurar_checklist` time.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, DocumentoRequisitoVinculo
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.requisito_documento import RequisitoDocumento
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CDP-001",
        objeto="Servicios CDP",
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


async def _make_cuenta(
    db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024, requisitos_modo: str | None = None
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo=requisitos_modo,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _make_documento_fuente(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    cuenta: CuentaCobro,
    nombre: str,
    tipo: TipoDocumentoFuente = TipoDocumentoFuente.RPC,
) -> DocumentoFuente:
    df = DocumentoFuente(
        usuario_id=test_user["user"].id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        storage_key=f"k/{nombre}",
        nombre=nombre,
        tipo=tipo,
    )
    db.add(df)
    await db.commit()
    await db.refresh(df)
    return df


async def _fila(
    db: AsyncSession, cuenta_id, codigo: str | None = None, requisito_cuenta_id=None
) -> DocumentoCuentaCobro:
    stmt = select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id)
    if codigo is not None:
        stmt = stmt.where(DocumentoCuentaCobro.requisito_codigo == codigo)
    else:
        stmt = stmt.where(DocumentoCuentaCobro.requisito_cuenta_id == requisito_cuenta_id)
    res = await db.execute(stmt)
    return res.scalar_one()


async def _vinculos_de(db: AsyncSession, documento_cuenta_cobro_id) -> list[DocumentoRequisitoVinculo]:
    res = await db.execute(
        select(DocumentoRequisitoVinculo).where(
            DocumentoRequisitoVinculo.documento_cuenta_cobro_id == documento_cuenta_cobro_id
        )
    )
    return list(res.scalars().all())


# ── CDP is a first-class nivel-contrato code ───────────────────────────────


async def test_es_nivel_contrato_cdp_true() -> None:
    assert checklist_service.es_nivel_contrato("CDP") is True


# ── Catalog seed: additive, insert-if-absent ───────────────────────────────


async def test_catalogo_seed_incluye_cdp_no_obligatorio_orden_25(db: AsyncSession) -> None:
    catalogo = await checklist_service.listar_catalogo(db)
    cdp = next((c for c in catalogo if c.codigo == "CDP"), None)
    assert cdp is not None
    assert cdp.obligatorio is False
    assert cdp.orden == 25


async def test_catalogo_seed_insertar_si_ausente_cuando_catalogo_ya_sembrado(db: AsyncSession) -> None:
    """Simulates a catalog already seeded WITHOUT CDP (e.g. an older
    deployment). Seeding must be additive: the empty-table early-return in
    `_seed_catalogo_si_vacio` must not block adding the missing CDP row."""
    for item in checklist_service._CATALOGO_SEED:
        if item["codigo"] == "CDP":
            continue
        db.add(RequisitoDocumento(**item))
    await db.commit()

    catalogo = await checklist_service.listar_catalogo(db)
    codigos = [c.codigo for c in catalogo]
    assert codigos.count("CDP") == 1

    # Idempotent: calling again does not add a second CDP row.
    catalogo2 = await checklist_service.listar_catalogo(db)
    assert [c.codigo for c in catalogo2].count("CDP") == 1


# ── Alias detection: RPC-folded document ───────────────────────────────────


async def test_alias_detection_links_rpc_folded_cdp_document_sin_quitar_rpc(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    df = await _make_documento_fuente(db, test_user, contrato, cuenta, "RPC incluye disponibilidad presupuestal.pdf")
    await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    # Re-running asegurar_checklist triggers the lazy alias detection pass.
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    rpc_fila = await _fila(db, cuenta.id, codigo="RPC")
    cdp_fila = await _fila(db, cuenta.id, codigo="CDP")

    rpc_vinculos = await _vinculos_de(db, rpc_fila.id)
    cdp_vinculos = await _vinculos_de(db, cdp_fila.id)

    # RPC link is kept — not moved, not removed.
    assert rpc_fila.documento_fuente_id == df.id
    assert {v.documento_fuente_id for v in rpc_vinculos} == {df.id}

    # CDP additionally links the SAME document.
    assert {v.documento_fuente_id for v in cdp_vinculos} == {df.id}


async def test_alias_detection_no_folds_rpc_document_sin_keywords_cdp(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """An RPC document that does NOT mention CDP-family keywords must not be
    aliased — detection is keyword-distinct from RPC ('compromiso presupuestal'
    vs 'disponibilidad presupuestal')."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    df = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-compromiso-presupuestal.pdf")
    await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    cdp_fila = await _fila(db, cuenta.id, codigo="CDP")
    cdp_vinculos = await _vinculos_de(db, cdp_fila.id)
    assert cdp_vinculos == []


async def test_alias_detection_es_idempotente_sin_duplicar_vinculos(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    df = await _make_documento_fuente(db, test_user, contrato, cuenta, "RPC disponibilidad presupuestal.pdf")
    await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    # Run asegurar_checklist (which triggers alias detection) three times.
    for _ in range(3):
        await checklist_service.asegurar_checklist(db, cuenta)
        await db.commit()

    cdp_fila = await _fila(db, cuenta.id, codigo="CDP")
    cdp_vinculos = await _vinculos_de(db, cdp_fila.id)
    assert len(cdp_vinculos) == 1


# ── Alias detection: custom RequisitoCuenta ─────────────────────────────────


async def _cuenta_con_custom_cdp(db: AsyncSession, contrato: Contrato) -> tuple[CuentaCobro, RequisitoCuenta]:
    cuenta = await _make_cuenta(db, contrato, mes=1, requisitos_modo="augment")

    rc = RequisitoCuenta(
        cuenta_cobro_id=cuenta.id,
        codigo="CDP_CUSTOM",
        etiqueta="Certificado de disponibilidad presupuestal",
        obligatorio=True,
        keywords_deteccion=["disponibilidad presupuestal"],
        orden=500,
        origen="inferido",
        activo=True,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    return cuenta, rc


async def test_alias_detection_custom_requisito_matching_cdp_keywords(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta, rc = await _cuenta_con_custom_cdp(db, contrato)

    df = await _make_documento_fuente(
        db, test_user, contrato, cuenta, "certificado-disponibilidad.pdf", tipo=TipoDocumentoFuente.RPC
    )
    await checklist_service.vincular_documento_fuente(db, cuenta.id, str(rc.id), df.id)
    await db.commit()

    # Re-run to trigger the lazy alias detection pass.
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    custom_fila = await _fila(db, cuenta.id, requisito_cuenta_id=rc.id)
    cdp_fila = await _fila(db, cuenta.id, codigo="CDP")

    custom_vinculos = await _vinculos_de(db, custom_fila.id)
    cdp_vinculos = await _vinculos_de(db, cdp_fila.id)

    # Custom row is left in place, untouched — not deleted, not converted.
    assert custom_fila.documento_fuente_id == df.id
    assert {v.documento_fuente_id for v in custom_vinculos} == {df.id}

    # Standard CDP row additionally links the same document.
    assert {v.documento_fuente_id for v in cdp_vinculos} == {df.id}

    # The custom RequisitoCuenta definition row itself still exists.
    still_exists = await db.execute(select(RequisitoCuenta).where(RequisitoCuenta.id == rc.id))
    assert still_exists.scalar_one_or_none() is not None
