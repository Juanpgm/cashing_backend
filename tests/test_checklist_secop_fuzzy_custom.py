"""Tests for fuzzy SECOP matching + custom-requisito auto-detection.

Two capabilities:
  1. `_secop_documentos_del_contrato` tolerates hand-entry differences (case,
     spaces, accents, special chars, leading zeros) and matches via multiple
     identifiers (numero_contrato, referencia, proceso, cedula/documento_proveedor).
  2. Custom (per-cuenta) requisitos are scored by their `keywords_deteccion`
     against SECOP documents and uploaded documents, with CONSERVATIVE auto-link
     (only the top match at/above the threshold; weaker matches are candidates).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.core import text_match
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.secop import SecopContrato, SecopDocumento
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── text_match unit tests ────────────────────────────────────────────────────


async def test_solo_digitos() -> None:
    assert text_match.solo_digitos("C.C. 01.234.567") == "1234567"
    assert text_match.solo_digitos("1234567") == "1234567"
    assert text_match.solo_digitos("000") == "0"
    assert text_match.solo_digitos("") == ""
    assert text_match.solo_digitos(None) == ""


async def test_similar() -> None:
    # Case, spaces and special characters collapse to the same core → exact.
    assert text_match.similar("CD-045-2025", "cd 045 2025") == Decimal("1.000")
    assert text_match.similar("CO1.PCCNTR.123", "co1 pccntr 123") == Decimal("1.000")
    # Accents are stripped.
    assert text_match.similar("Contrató", "contrato") == Decimal("1.000")
    # A near-miss scores high but below 1.000.
    assert Decimal("0.800") < text_match.similar("CD-045-2025", "CD-045-2026") < Decimal("1.000")
    # Unrelated identifiers score low.
    assert text_match.similar("CD-045-2025", "ZZ-999") < Decimal("0.500")
    assert text_match.similar("", "abc") == Decimal("0.000")


# ── Fixtures ─────────────────────────────────────────────────────────────────


async def _contrato(db: AsyncSession, user_id: Any, *, numero: str, documento_proveedor: str | None = None) -> Contrato:
    c = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Servicios profesionales para prueba SECOP fuzzy",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        documento_proveedor=documento_proveedor,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _secop_doc(
    db: AsyncSession,
    *,
    id_doc: str,
    numero_contrato: str | None = None,
    proceso: str | None = None,
    secop_contrato_id: Any | None = None,
    nombre: str = "documento.pdf",
    descripcion: str | None = None,
) -> SecopDocumento:
    d = SecopDocumento(
        id_documento_secop=id_doc,
        numero_contrato=numero_contrato,
        proceso=proceso,
        secop_contrato_id=secop_contrato_id,
        nombre_archivo=nombre,
        descripcion=descripcion,
        datos_raw={},
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


# ── Fuzzy SECOP lookup ───────────────────────────────────────────────────────


async def test_lookup_exacto(db: AsyncSession, test_user: dict[str, Any]) -> None:
    c = await _contrato(db, test_user["user"].id, numero="CD-045-2025")
    await _secop_doc(db, id_doc="D1", numero_contrato="CD-045-2025")
    docs = await checklist_service._secop_documentos_del_contrato(db, c)
    assert len(docs) == 1


async def test_lookup_tolerante_por_numero(db: AsyncSession, test_user: dict[str, Any]) -> None:
    # SECOP stored the number with different case/spacing than the user typed.
    c = await _contrato(db, test_user["user"].id, numero="CD-045-2025")
    await _secop_doc(db, id_doc="D1", numero_contrato="cd 045 2025")
    docs = await checklist_service._secop_documentos_del_contrato(db, c)
    assert len(docs) == 1
    assert docs[0].id_documento_secop == "D1"


async def test_lookup_por_cedula_con_ceros(db: AsyncSession, test_user: dict[str, Any]) -> None:
    # No number match at all, but the contractor cedula matches (with leading zeros).
    c = await _contrato(db, test_user["user"].id, numero="INTERNO-XYZ", documento_proveedor="1234567")
    sc = SecopContrato(
        id_contrato_secop="SC1",
        cedula_contratista="01234567",  # leading zero vs the contract's 1234567
        numero_contrato="CO1.PCCNTR.999",
        referencia_del_contrato="CO1.PCCNTR.999",
        datos_raw={},
    )
    db.add(sc)
    await db.commit()
    await db.refresh(sc)
    await _secop_doc(db, id_doc="D1", numero_contrato="CO1.PCCNTR.999", secop_contrato_id=sc.id)
    docs = await checklist_service._secop_documentos_del_contrato(db, c)
    assert len(docs) == 1


async def test_lookup_sin_match_devuelve_vacio(db: AsyncSession, test_user: dict[str, Any]) -> None:
    c = await _contrato(db, test_user["user"].id, numero="CD-045-2025")
    await _secop_doc(db, id_doc="D1", numero_contrato="TOTALMENTE-DISTINTO-9999")
    docs = await checklist_service._secop_documentos_del_contrato(db, c)
    assert docs == []


# ── Custom requisito detection ───────────────────────────────────────────────


async def _cuenta_con_custom(
    db: AsyncSession,
    contrato: Contrato,
    *,
    codigo: str,
    keywords: list[str],
) -> tuple[CuentaCobro, RequisitoCuenta]:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=5,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="augment",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)

    rc = RequisitoCuenta(
        cuenta_cobro_id=cc.id,
        codigo=codigo,
        etiqueta=codigo.replace("_", " ").title(),
        obligatorio=True,
        keywords_deteccion=keywords,
        orden=500,
        origen="inferido",
        activo=True,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)

    await checklist_service.asegurar_checklist(db, cc)
    await db.commit()
    return cc, rc


async def _fila_custom(db: AsyncSession, cuenta: CuentaCobro, rc: RequisitoCuenta) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_cuenta_id == rc.id,
        )
    )
    return res.scalar_one()


async def test_custom_secop_autolink_fuerte(db: AsyncSession, test_user: dict[str, Any]) -> None:
    c = await _contrato(db, test_user["user"].id, numero="CD-100-2024")
    cc, rc = await _cuenta_con_custom(db, c, codigo="POLIZA_CUMPLIMIENTO", keywords=["poliza", "cumplimiento"])
    await _secop_doc(
        db,
        id_doc="D1",
        numero_contrato="CD-100-2024",
        nombre="Poliza de cumplimiento No 123.pdf",
        descripcion="Garantia de cumplimiento",
    )
    await checklist_service.detectar_desde_secop(db, cc)
    await db.commit()

    fila = await _fila_custom(db, cc, rc)
    # 2/2 keywords → 1.000 ≥ threshold → auto-linked as DETECTADO.
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id is not None


async def test_custom_secop_debil_no_autolink_pero_es_candidato(db: AsyncSession, test_user: dict[str, Any]) -> None:
    c = await _contrato(db, test_user["user"].id, numero="CD-101-2024")
    cc, rc = await _cuenta_con_custom(db, c, codigo="ANEXO_TECNICO", keywords=["anexo", "tecnico", "especificaciones"])
    await _secop_doc(
        db,
        id_doc="D1",
        numero_contrato="CD-101-2024",
        nombre="Anexo general.pdf",
        descripcion="Documento anexo",
    )
    await checklist_service.detectar_desde_secop(db, cc)
    await db.commit()

    fila = await _fila_custom(db, cc, rc)
    # 1/3 keywords → 0.333 < 0.700 → conservative: NOT auto-linked.
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.secop_documento_id is None

    # ...but it must surface as a candidate so the user can link it manually.
    payload = await checklist_service.construir_checklist_completo(db, cc)
    item = next(i for i in payload["items"] if i["requisito"]["requisito_cuenta_id"] == rc.id)
    assert len(item["candidatos_secop"]) >= 1
    assert item["candidatos_secop"][0]["nombre_archivo"] == "Anexo general.pdf"


async def test_custom_documento_fuente_autolink_por_keywords(db: AsyncSession, test_user: dict[str, Any]) -> None:
    c = await _contrato(db, test_user["user"].id, numero="CD-102-2024")
    cc, rc = await _cuenta_con_custom(db, c, codigo="POLIZA_CUMPLIMIENTO", keywords=["poliza", "cumplimiento"])
    df = DocumentoFuente(
        usuario_id=test_user["user"].id,
        contrato_id=c.id,
        cuenta_cobro_id=cc.id,
        storage_key="k/poliza.pdf",
        nombre="Poliza de cumplimiento firmada.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
    )
    db.add(df)
    await db.commit()

    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cc)
    await db.commit()

    assert vinculados >= 1
    fila = await _fila_custom(db, cc, rc)
    assert fila.estado == EstadoRequisito.CARGADO
    assert fila.documento_fuente_id == df.id
