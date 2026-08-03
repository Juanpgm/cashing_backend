"""R6 integration tests (clasificacion-documentos-refuerzo): the categoria-driven
auto-link path in `detectar_desde_secop`.

`detectar_desde_secop` already gated auto-link on `categoria_confianza >=
AUTO_LINK_THRESHOLD` (R6, no code change) — what changed is what
`categoria_confianza` actually IS: R2 (primary/secondary scoring) now lets a
confident filename clear 0.700 via `document_classifier.aplicar_clasificacion`,
and R3 (dominance guard) demotes a genuinely ambiguous RP/CDP filename below
it. These tests classify the SecopDocumento THROUGH `aplicar_clasificacion`
(mirroring what `secop_service._upsert_documento` does on every SECOP import)
so the categoria-based candidate branch of `detectar_desde_secop`
(app/services/checklist_service.py ~L1107-1118) is actually exercised — the
other checklist_secop_* suites mostly leave `categoria` at its OTROS default
and exercise the separate keywords_deteccion fallback branch instead.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.secop import SecopDocumento
from app.services import checklist_service
from app.services.document_classifier import aplicar_clasificacion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-CATAUTOLINK-001",
        objeto="Servicios de clasificacion",
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


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int = 1) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    await checklist_service.asegurar_checklist(db, cuenta=cc)
    await db.commit()
    return cc


def _secop_doc_clasificado(contrato: Contrato, nombre: str) -> SecopDocumento:
    """Build+classify a SecopDocumento exactly like secop_service._upsert_documento
    does on import: aplicar_clasificacion runs BEFORE the row is persisted."""
    doc = SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
    )
    aplicar_clasificacion(doc)
    return doc


async def _fila(db: AsyncSession, cuenta_id: uuid.UUID, codigo: str) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo == codigo,
        )
    )
    return res.scalar_one()


@pytest.fixture(autouse=True)
def _sin_extraccion_obligaciones(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate these tests from the (LLM-backed) obligaciones auto-extraction
    trigger — its own dedicated suite is test_checklist_secop_obligaciones_autotrigger.py."""
    from app.services import document_service

    async def _noop(texto: str, contrato_id: Any, db: Any) -> tuple[list, list]:
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _noop)


async def test_contrato_confiado_y_dominante_autolinkea(db: AsyncSession, contrato: Contrato) -> None:
    doc = _secop_doc_clasificado(contrato, "Contrato de prestacion de servicios.pdf")
    db.add(doc)
    await db.commit()
    assert doc.categoria_confianza is not None
    assert doc.categoria_confianza >= 0.700

    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id


async def test_rp_confiado_y_dominante_autolinkea(db: AsyncSession, contrato: Contrato) -> None:
    doc = _secop_doc_clasificado(contrato, "RPC 12345 registro presupuestal.pdf")
    db.add(doc)
    await db.commit()

    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "RPC")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id


async def test_cdp_confiado_y_dominante_autolinkea(db: AsyncSession, contrato: Contrato) -> None:
    doc = _secop_doc_clasificado(contrato, "Certificado de Disponibilidad Presupuestal.pdf")
    db.add(doc)
    await db.commit()

    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "CDP")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id


async def test_acta_inicio_con_contrato_generico_autolinkea(db: AsyncSession, contrato: Contrato) -> None:
    """Correction (RELIABILITY-001): a real SECOP ACTA_INICIO filename that also
    mentions the generic word 'contrato' must still auto-link — that co-occurrence
    is not a genuine ambiguity threat."""
    doc = _secop_doc_clasificado(contrato, "ACTA DE INICIO DEL CONTRATO No 123.pdf")
    db.add(doc)
    await db.commit()
    assert doc.categoria_confianza is not None
    assert doc.categoria_confianza >= 0.700

    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "ACTA_INICIO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id


async def test_rp_cdp_ambiguo_no_autolinkea_queda_como_candidato(db: AsyncSession, contrato: Contrato) -> None:
    """Both RP and CDP PRIMARY keywords present, neither dominates (R3) — must
    NOT auto-link either requisito; stays PENDIENTE, surfaced only as a candidate."""
    doc = _secop_doc_clasificado(contrato, "RPC anexo certificado de disponibilidad.pdf")
    db.add(doc)
    await db.commit()
    assert doc.categoria_confianza == pytest.approx(0.600)

    cuenta = await _make_cuenta(db, contrato)
    resultado = await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila_rp = await _fila(db, cuenta.id, "RPC")
    fila_cdp = await _fila(db, cuenta.id, "CDP")
    assert fila_rp.estado == EstadoRequisito.PENDIENTE
    assert fila_cdp.estado == EstadoRequisito.PENDIENTE
    # The ambiguous doc still surfaces as a candidate for whichever requisito
    # document_classifier picked as best_cat (manual Vincular), just not auto-linked.
    candidato_en = [codigo for codigo in ("RPC", "CDP") if any(d.id == doc.id for d, _ in resultado.get(codigo, []))]
    assert candidato_en, "el documento ambiguo debe aparecer como candidato de RPC o CDP"


async def test_contrato_confiado_dispara_extraccion_de_obligaciones(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6: the categoria-driven CONTRATO auto-link also fires the same task-3.9
    obligaciones-extraction trigger as the keywords_deteccion-fallback path."""
    from app.services import document_service

    llamadas: list[Any] = []

    async def _spy(texto: str, contrato_id: Any, db_inner: Any) -> tuple[list, list]:
        llamadas.append(contrato_id)
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _spy)

    doc = _secop_doc_clasificado(contrato, "Contrato de prestacion de servicios.pdf")
    doc.texto_extraido = "Contrato de prestacion de servicios."
    doc.texto_estado = "ok"
    db.add(doc)
    await db.commit()

    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert llamadas == [contrato.id]
