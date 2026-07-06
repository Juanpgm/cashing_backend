"""Tests for app.services.checklist_service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    EstadoRequisito,
)
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CHK-001",
        objeto="Servicios de checklist",
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
    db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── asegurar_checklist ─────────────────────────────────────────────────────


async def test_asegurar_checklist_creates_rows_first_cuenta(
    db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)

    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    # First cuenta → recurring + first-only requisitos
    assert "CONTRATO" in codigos
    assert "RPC" in codigos
    assert "SEGURIDAD_SOCIAL" in codigos
    assert "CEDULA" in codigos  # first-only
    assert "RUT" in codigos  # first-only
    assert "ACTA_INICIO" in codigos


async def test_asegurar_checklist_contract_level_appears_every_cuenta(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Contract-level requisitos (CONTRATO, RUT, CEDULA, ACTA_INICIO) appear on EVERY
    cuenta — they are auto-fulfilled by the shared contract-level document, so
    solo_primera_cuenta no longer hides them on later cuentas."""
    # Earlier cuenta
    await _make_cuenta(db, contrato, mes=1)
    # Later cuenta
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CONTRATO" in codigos
    assert "CEDULA" in codigos  # contract-level → shared, appears on every cuenta
    assert "RUT" in codigos
    assert "ACTA_INICIO" in codigos


async def test_asegurar_checklist_is_idempotent(
    db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)

    filas1 = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    filas2 = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    assert len(filas1) == len(filas2)
    # No duplicate rows in DB
    from sqlalchemy import select

    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id
        )
    )
    rows = list(res.scalars().all())
    codigos = [r.requisito_codigo for r in rows]
    assert len(codigos) == len(set(codigos))


async def test_new_cuenta_does_not_inherit_old_cuenta_links(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Two-tier model: a new cuenta never copies stale LINKS, but contract-level
    documents (shared) re-derive via auto_vincular while cuenta-level documents
    (scoped to another cuenta) never leak in.
    """
    from sqlalchemy import select

    user = test_user["user"]

    async def _fila(cuenta_id, codigo):
        r = await db.execute(
            select(DocumentoCuentaCobro).where(
                DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
                DocumentoCuentaCobro.requisito_codigo == codigo,
            )
        )
        return r.scalar_one()

    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta1)

    # Contract-level document (CONTRATO): shared, cuenta_cobro_id NULL.
    df_contrato = DocumentoFuente(
        usuario_id=user.id, contrato_id=contrato.id, cuenta_cobro_id=None,
        storage_key="k/contrato", nombre="contrato.pdf", tipo=TipoDocumentoFuente.CONTRATO,
    )
    # Cuenta-level document (SEGURIDAD_SOCIAL): strictly scoped to cuenta1.
    df_cuenta = DocumentoFuente(
        usuario_id=user.id, contrato_id=contrato.id, cuenta_cobro_id=cuenta1.id,
        storage_key="k/ss", nombre="planilla.pdf", tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
    )
    db.add_all([df_contrato, df_cuenta])
    await db.commit()

    # Second cuenta: rows start PENDIENTE — no link copied over.
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()
    assert (await _fila(cuenta2.id, "CONTRATO")).estado == EstadoRequisito.PENDIENTE

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta2)
    await db.commit()

    # Contract-level CONTRATO re-derives from the shared document → CARGADO.
    assert (await _fila(cuenta2.id, "CONTRATO")).estado == EstadoRequisito.CARGADO
    # Cuenta-level SEGURIDAD_SOCIAL (scoped to cuenta1) never leaks into cuenta2.
    assert (await _fila(cuenta2.id, "SEGURIDAD_SOCIAL")).estado == EstadoRequisito.PENDIENTE


# ── detectar_desde_secop ───────────────────────────────────────────────────


async def test_detectar_desde_secop_scores_and_autolinks(
    db: AsyncSession, contrato: Contrato
) -> None:
    # Seed SECOP docs with names that match keywords for distinct requisitos
    doc_contrato = SecopDocumento(
        id_documento_secop="DOC-1",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="Contrato firmado minuta clausulado.pdf",
        descripcion="Contrato",
        datos_raw={},
    )
    doc_rpc = SecopDocumento(
        id_documento_secop="DOC-2",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="RPC registro presupuestal compromiso presupuestal.pdf",
        descripcion="RP",
        datos_raw={},
    )
    doc_unrelated = SecopDocumento(
        id_documento_secop="DOC-3",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="Anexo Z.pdf",
        descripcion="",
        datos_raw={},
    )
    db.add_all([doc_contrato, doc_rpc, doc_unrelated])
    await db.commit()

    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    result = await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert "CONTRATO" in result
    assert "RPC" in result
    # Top score for CONTRATO should be the contrato doc
    top_contrato_doc, top_score_contrato = result["CONTRATO"][0]
    assert top_contrato_doc.id == doc_contrato.id
    assert top_score_contrato >= Decimal("0.700")

    # Check row was auto-linked
    from sqlalchemy import select

    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == "CONTRATO",
        )
    )
    fila = res.scalar_one()
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc_contrato.id

    # Candidate rows persisted
    cand_res = await db.execute(
        select(DocumentoChecklistCandidato).where(
            DocumentoChecklistCandidato.cuenta_cobro_id == cuenta.id,
            DocumentoChecklistCandidato.requisito_codigo == "CONTRATO",
        )
    )
    candidatos = list(cand_res.scalars().all())
    assert len(candidatos) >= 1


# ── manual transitions ─────────────────────────────────────────────────────


async def test_marcar_no_aplica_and_cumplido_manual(
    db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    fila = await checklist_service.marcar_no_aplica(db, cuenta.id, "DS_CONSECUTIVO")
    await db.commit()
    assert fila.estado == EstadoRequisito.NO_APLICA

    fila2 = await checklist_service.marcar_cumplido_manual(
        db, cuenta.id, "COMPROBANTE_PAGO_SS"
    )
    await db.commit()
    assert fila2.estado == EstadoRequisito.CUMPLIDO_MANUAL


# ── resumen ────────────────────────────────────────────────────────────────


async def test_computar_resumen_marks_radicacion_lista_when_complete(
    db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    catalogo = await checklist_service.listar_catalogo(db)

    from sqlalchemy import select

    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id
        )
    )
    filas = list(res.scalars().all())

    # Mark all obligatorios as cumplido_manual or no_aplica
    for fila in filas:
        req = next(c for c in catalogo if c.codigo == fila.requisito_codigo)
        if req.obligatorio:
            fila.estado = EstadoRequisito.CUMPLIDO_MANUAL
        else:
            fila.estado = EstadoRequisito.NO_APLICA
    await db.commit()

    resumen = checklist_service.computar_resumen(filas, catalogo)
    assert resumen["pendientes"] == 0
    assert resumen["radicacion_lista"] is True
    assert resumen["cumplidos"] == resumen["total"]


async def test_computar_resumen_radicacion_no_lista_si_falta(
    db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    resumen = checklist_service.computar_resumen(filas, catalogo)
    assert resumen["pendientes"] > 0
    assert resumen["radicacion_lista"] is False
