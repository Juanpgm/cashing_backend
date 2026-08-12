"""`contrato_service.vincular_secop_documento` — manual Vincular SECOP doc service.

design-technical.md steps 1-5: owner-scoped contrato load, SecopDocumento load,
numero_contrato equality validation, delegate to the raising core
(`checklist_service.extraer_obligaciones_desde_secop_doc`), return its result.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from app.core.exceptions import NotFoundError, ValidationError
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.secop import SecopContrato, SecopDocumento
from app.schemas.agent import ObligacionExtraida
from app.services import checklist_service, contrato_service
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
        numero_contrato="CTR-VINC-SVC-001",
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


def _secop_doc(numero_contrato: str, nombre: str = "documento.docx", **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
        **kwargs,
    )


async def test_contrato_no_encontrado_lanza_notfound(db: AsyncSession, test_user: dict[str, Any]) -> None:
    with pytest.raises(NotFoundError):
        await contrato_service.vincular_secop_documento(db, test_user["user"].id, uuid.uuid4(), uuid.uuid4())


async def test_contrato_de_otro_usuario_lanza_notfound(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    with pytest.raises(NotFoundError):
        await contrato_service.vincular_secop_documento(db, uuid.uuid4(), contrato.id, uuid.uuid4())


async def test_secop_documento_no_encontrado_lanza_notfound(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    with pytest.raises(NotFoundError):
        await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, uuid.uuid4())


async def test_numero_contrato_no_coincide_lanza_validation_error(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"core": False}

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[ObligacionExtraida], list[str]]:
        called["core"] = True
        return [], []

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)
    doc = _secop_doc("CTR-OTRO-CONTRATO")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(ValidationError):
        await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, doc.id)

    assert called["core"] is False


async def test_doc_numero_contrato_es_referencia_secop_contrato_permite_vincular(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validator 2 (D5): a SecopDocumento whose ``numero_contrato`` stores the
    SecopContrato's referencia_del_contrato (≠ Contrato.numero_contrato) is
    accepted once a SecopContrato row resolves that referencia back to this
    contrato via its own ``numero_contrato`` field."""
    called = {"core": False}

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[ObligacionExtraida], list[str]]:
        called["core"] = True
        return [], []

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    secop_contrato = SecopContrato(
        id_contrato_secop=f"SECOP-{uuid.uuid4().hex[:10]}",
        cedula_contratista="123456789",
        numero_contrato=contrato.numero_contrato,
        referencia_del_contrato="REF-99887766",
        datos_raw={},
    )
    db.add(secop_contrato)
    await db.commit()

    doc = _secop_doc("REF-99887766")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, doc.id)

    assert called["core"] is True


async def test_doc_vinculado_por_secop_contrato_id_via_proceso_permite_vincular(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validator 2 (D5): a SecopDocumento linked via ``secop_contrato_id`` FK to
    a SecopContrato resolved through ``proceso_de_compra`` is accepted even
    though the doc's own ``numero_contrato`` field is unrelated."""
    called = {"core": False}

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[ObligacionExtraida], list[str]]:
        called["core"] = True
        return [], []

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    secop_contrato = SecopContrato(
        id_contrato_secop=f"SECOP-{uuid.uuid4().hex[:10]}",
        cedula_contratista="123456789",
        proceso_de_compra=contrato.numero_contrato,
        datos_raw={},
    )
    db.add(secop_contrato)
    await db.commit()
    await db.refresh(secop_contrato)

    doc = _secop_doc("UNRELATED-NUM", secop_contrato_id=secop_contrato.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, doc.id)

    assert called["core"] is True


async def test_happy_path_delega_al_core_y_retorna_resultado(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    resultado = ([ObligacionExtraida(descripcion="Obligación X", tipo="general", orden=1)], ["aviso"])
    recibido: dict[str, Any] = {}

    async def _core_spy(db_arg: AsyncSession, contrato_arg: Contrato, doc_arg: SecopDocumento, **kwargs: Any):
        recibido["db"] = db_arg
        recibido["contrato"] = contrato_arg
        recibido["doc"] = doc_arg
        return resultado

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)
    doc = _secop_doc(contrato.numero_contrato)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    obligaciones, avisos = await contrato_service.vincular_secop_documento(
        db, test_user["user"].id, contrato.id, doc.id
    )

    assert (obligaciones, avisos) == resultado
    assert recibido["contrato"].id == contrato.id
    assert recibido["doc"].id == doc.id


async def test_timeout_de_extraccion_lanza_validation_error(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real `TimeoutError` from the raising core (checklist_service) must be
    mapped to a `ValidationError` (422) by the caller, not surface as a 500 or
    be swallowed (RELIABILITY-001)."""
    from app.services import document_service

    async def _lento(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        await asyncio.sleep(0.15)
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _lento)
    monkeypatch.setattr(checklist_service, "_AUTO_OBLIGACIONES_TIMEOUT_SEGUNDOS", 0.01)

    doc = _secop_doc(contrato.numero_contrato, "contrato.docx", texto_estado="ok", texto_extraido=TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(ValidationError):
        await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, doc.id)


async def _fila_documento_fuente_contrato(db: AsyncSession, contrato_id: uuid.UUID) -> list[DocumentoFuente]:
    result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
        )
    )
    return list(result.scalars().all())


async def test_vincular_persiste_documento_fuente_contrato_si_falta(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live-reproduced bug: Vincular downloads a SECOP doc's bytes to extract
    obligaciones but never persisted a `DocumentoFuente` from it, so Checklist
    (which reads `DocumentoFuente` directly) permanently showed
    'Contrato / minuta / clausulado — Pendiente' even after obligaciones existed."""

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[ObligacionExtraida], list[str]]:
        return ([ObligacionExtraida(descripcion="Obligación X", tipo="general", orden=1)], [])

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    descargas: dict[str, int] = {"n": 0}

    async def _fake_download(url: str, timeout: float = 0) -> bytes:
        descargas["n"] += 1
        return b"fake bytes"

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_download)

    mock_storage = AsyncMock()
    mock_storage.upload = AsyncMock()
    monkeypatch.setattr(checklist_service, "_get_storage", lambda bucket: mock_storage)

    doc = _secop_doc(
        contrato.numero_contrato,
        "contrato.docx",
        url_descarga="https://secop/fake.docx",
        texto_estado="ok",
        texto_extraido=TEXTO_CONTRATO,
        categoria=CategoriaDocumento.CONTRATO,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, doc.id)

    docs = await _fila_documento_fuente_contrato(db, contrato.id)
    assert len(docs) == 1
    assert docs[0].cuenta_cobro_id is None
    assert docs[0].texto_extraido == TEXTO_CONTRATO
    assert docs[0].categoria == CategoriaDocumento.CONTRATO
    assert descargas["n"] == 1
    mock_storage.upload.assert_awaited_once()


async def test_vincular_idempotente_no_duplica_documento_fuente(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CONTRATO DocumentoFuente already exists for this contrato → no-op:
    no re-download, no re-upload, no duplicate row."""
    existente = DocumentoFuente(
        usuario_id=test_user["user"].id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,
        storage_key="usuarios/x/documentos/y/existente.docx",
        nombre="existente.docx",
        tipo=TipoDocumentoFuente.CONTRATO,
        texto_extraido="ya existente",
    )
    db.add(existente)
    await db.commit()

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[ObligacionExtraida], list[str]]:
        return ([], [])

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    download_spy = AsyncMock(side_effect=AssertionError("no debe descargar"))
    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", download_spy)
    storage_factory_spy = AsyncMock(side_effect=AssertionError("no debe subir a storage"))
    monkeypatch.setattr(checklist_service, "_get_storage", storage_factory_spy)

    doc = _secop_doc(
        contrato.numero_contrato,
        "contrato.docx",
        url_descarga="https://secop/fake.docx",
        texto_estado="ok",
        texto_extraido=TEXTO_CONTRATO,
        categoria=CategoriaDocumento.CONTRATO,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await contrato_service.vincular_secop_documento(db, test_user["user"].id, contrato.id, doc.id)

    docs = await _fila_documento_fuente_contrato(db, contrato.id)
    assert len(docs) == 1
    download_spy.assert_not_awaited()
    storage_factory_spy.assert_not_awaited()


async def test_vincular_degrada_si_falla_persistencia_documento(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistence best-effort: a download failure must not raise nor block the
    obligaciones-extraction result the user is actually waiting on, and must not
    leave a partial DocumentoFuente row behind."""
    resultado = ([ObligacionExtraida(descripcion="Obligación X", tipo="general", orden=1)], [])

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[ObligacionExtraida], list[str]]:
        return resultado

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    async def _fallo_descarga(url: str, timeout: float = 0) -> bytes:
        raise httpx.TimeoutException("boom")

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fallo_descarga)

    doc = _secop_doc(
        contrato.numero_contrato,
        "contrato.docx",
        url_descarga="https://secop/fake.docx",
        texto_estado="ok",
        texto_extraido=TEXTO_CONTRATO,
        categoria=CategoriaDocumento.CONTRATO,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    obligaciones, avisos = await contrato_service.vincular_secop_documento(
        db, test_user["user"].id, contrato.id, doc.id
    )

    assert (obligaciones, avisos) == resultado
    docs = await _fila_documento_fuente_contrato(db, contrato.id)
    assert docs == []
