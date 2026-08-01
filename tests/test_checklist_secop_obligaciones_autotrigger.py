"""Auto-trigger obligaciones extraction on SECOP Contrato auto-assign (task 3.9).

When `detectar_desde_secop` auto-links the CONTRATO requisito and the contrato
has zero obligaciones, the same extraction an uploaded contrato gets
(`document_service.extraer_obligaciones_texto`) runs on the SECOP doc's
persisted `texto_extraido` (extracted at the trigger point with the bounded
sniff helpers if never sniffed). Idempotent, degraded on failure, and never
fired for manual links.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from typing import Any

import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


TEXTO_CONTRATO = (
    "CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES No. 99\n"
    "Entre la entidad y el contratista se celebra el presente contrato, cuyo clausulado se detalla."
)


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-AUTOOB-001",
        objeto="Servicios de extracción",
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


def _secop_doc(contrato: Contrato, nombre: str, **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
        **kwargs,
    )


def _docx_bytes(texto: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(texto)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def descargas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    llamadas: list[str] = []
    payloads: dict[str, bytes] = {}

    async def _fake(url: str) -> bytes:
        llamadas.append(url)
        return payloads[url]

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake)
    return {"llamadas": llamadas, "payloads": payloads}


@pytest.fixture
def extraccion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Spy on the extraction seam; persists one Obligacion like the real fn."""
    from app.services import document_service

    llamadas: list[tuple[str, uuid.UUID]] = []
    estado = {"falla": False}

    async def _fake(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        llamadas.append((texto, contrato_id))
        if estado["falla"]:
            raise RuntimeError("extracción fallida simulada")
        db.add(
            Obligacion(
                contrato_id=contrato_id,
                descripcion="Obligación extraída del contrato SECOP",
                tipo=TipoObligacion.GENERAL,
                orden=1,
            )
        )
        await db.flush()
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _fake)
    return {"llamadas": llamadas, "estado": estado}


async def _fila_contrato(db: AsyncSession, cuenta_id: uuid.UUID) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo == "CONTRATO",
        )
    )
    return res.scalar_one()


async def _obligaciones(db: AsyncSession, contrato_id: uuid.UUID) -> list[Obligacion]:
    res = await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato_id))
    return list(res.scalars().all())


async def test_autolink_por_sniff_dispara_extraccion(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """Borderline doc sniffed as CONTRATO → auto-link → extraction runs on the
    persisted texto_extraido for THIS contrato."""
    url = "https://s/generico.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(contrato, "documento_generico.docx", url_descarga=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO
    assert len(extraccion["llamadas"]) == 1
    texto, contrato_id = extraccion["llamadas"][0]
    assert "CONTRATO DE PRESTACI" in texto
    assert contrato_id == contrato.id
    obligaciones = await _obligaciones(db, contrato.id)
    assert [ob.descripcion for ob in obligaciones] == ["Obligación extraída del contrato SECOP"]


async def test_autolink_confiado_extrae_texto_en_ese_punto(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """A confident filename auto-link never went through the sniff — the trigger
    extracts the text at that point with the same bounded download helpers."""
    url = "https://s/contrato.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(contrato, "Contrato firmado minuta clausulado.docx", url_descarga=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == [url]  # downloaded once, at the trigger point
    await db.refresh(doc)
    assert doc.texto_estado == "ok"
    assert len(extraccion["llamadas"]) == 1
    assert len(await _obligaciones(db, contrato.id)) == 1


async def test_rerun_no_duplica_obligaciones(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """Once the contrato has obligaciones, a re-triggered auto-link never
    re-runs the extraction."""
    url = "https://s/generico.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(contrato, "documento_generico.docx", url_descarga=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()
    assert len(extraccion["llamadas"]) == 1

    # Unlink and re-scan: the auto-link fires again, the extraction must not.
    await checklist_service.desvincular(db, cuenta.id, "CONTRATO")
    await db.commit()
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO  # re-linked
    assert len(extraccion["llamadas"]) == 1  # but NOT re-extracted
    assert len(await _obligaciones(db, contrato.id)) == 1


async def test_fallo_de_extraccion_no_rompe_deteccion(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    extraccion["estado"]["falla"] = True
    url = "https://s/generico.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(contrato, "documento_generico.docx", url_descarga=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    resultado = await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    # Detection completed and the auto-link stands despite the extraction error.
    assert "CONTRATO" in resultado
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id
    assert len(extraccion["llamadas"]) == 1  # attempted, failed, degraded
    assert await _obligaciones(db, contrato.id) == []


async def test_vinculo_manual_no_dispara_extraccion(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """A manually linked Contrato is never re-processed by the trigger.

    (The PR1 sniff may still legitimately download the doc while evaluating the
    PENDIENTE RPC/CDP filas — only the extraction trigger is under test here.)
    """
    url = "https://s/manual.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(contrato, "documento_generico.docx", url_descarga=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.vincular_secop_documento(db, cuenta.id, "CONTRATO", doc.id)
    await db.commit()

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert extraccion["llamadas"] == []
    assert await _obligaciones(db, contrato.id) == []


async def test_doc_sin_texto_degrada_sin_extraccion(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """Auto-link of a doc whose text cannot be recovered (no url) degrades:
    texto_estado persisted, no extraction, detection intact."""
    doc = _secop_doc(
        contrato,
        "documento_generico.docx",
        categoria=CategoriaDocumento.CONTRATO,
        categoria_override=True,  # scores 1.000 → auto-link without sniff
    )
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO
    await db.refresh(doc)
    assert doc.texto_estado == "error"  # no url_descarga → unrecoverable, memoized
    assert extraccion["llamadas"] == []
    assert await _obligaciones(db, contrato.id) == []
