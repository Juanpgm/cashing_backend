"""Raising core for manual Vincular SECOP doc -> obligaciones extraction.

`checklist_service.extraer_obligaciones_desde_secop_doc` is the RAISING sibling of
the swallowing `_auto_extraer_obligaciones_contrato` (task 3.9): same ensure-text +
extract pipeline, but propagates failures instead of degrading -- required to back
a user-facing endpoint (Vincular) that must report a 4xx instead of a misleading
200 (design-technical.md, decision D2).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

import httpx
import pytest
from app.core.exceptions import ValidationError
from app.models.contrato import Contrato
from app.models.secop import SecopDocumento
from app.schemas.agent import ObligacionExtraida
from app.services import checklist_service
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
        numero_contrato="CTR-VINC-CORE-001",
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


def _secop_doc(contrato: Contrato, nombre: str, **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
        **kwargs,
    )


@pytest.fixture
def extraccion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Spy on `document_service.extraer_obligaciones_texto`."""
    from app.services import document_service

    llamadas: list[tuple[str, uuid.UUID]] = []
    resultado = ([ObligacionExtraida(descripcion="Obligación vinculada", tipo="general", orden=1)], [])

    async def _fake(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        llamadas.append((texto, contrato_id))
        return resultado

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _fake)
    return {"llamadas": llamadas, "resultado": resultado}


async def test_happy_path_retorna_obligaciones_y_avisos(
    db: AsyncSession, contrato: Contrato, extraccion: dict[str, Any]
) -> None:
    """Memoized `texto_estado='ok'` doc: no download, delegates straight to
    `document_service.extraer_obligaciones_texto` and returns its result."""
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="ok", texto_extraido=TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()

    obligaciones, avisos = await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc)

    assert (obligaciones, avisos) == extraccion["resultado"]
    assert extraccion["llamadas"] == [(TEXTO_CONTRATO, contrato.id)]


async def test_sin_texto_utilizable_lanza_excepcion_de_dominio(
    db: AsyncSession, contrato: Contrato, extraccion: dict[str, Any]
) -> None:
    """A doc whose text could not be recovered (memoized `sin_texto`/`error`,
    or missing content) RAISES instead of silently degrading."""
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="sin_texto", texto_extraido=None)
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc)

    assert extraccion["llamadas"] == []


async def test_timeout_de_extraccion_lanza_timeouterror(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow `extraer_obligaciones_texto` past the deadline raises `TimeoutError`
    -- callers (the manual Vincular service) decide how to map it to a 4xx; the
    scan's degraded wrapper keeps its own specific timeout log/degrade branch."""
    from app.services import document_service

    async def _lento(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        await asyncio.sleep(0.15)
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _lento)
    monkeypatch.setattr(checklist_service, "_AUTO_OBLIGACIONES_TIMEOUT_SEGUNDOS", 0.01)
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="ok", texto_extraido=TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()

    with pytest.raises(TimeoutError):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc)


# ── manual=True: dedicated acquisition for the user-facing Vincular action ──
#
# The manual path must NOT reuse the scan-oriented `_asegurar_texto_extraido`
# (stingy 10s/20s-clamped budget, memoizes 'sin_texto'/'error' as final) — a
# deliberate single-document user action gets a generous timeout, a fresh
# attempt even over a stale scan verdict, and a specific message per failure.


async def test_manual_reutiliza_texto_ya_extraido_sin_descargar(
    db: AsyncSession, contrato: Contrato, extraccion: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`texto_estado='ok'` with text already persisted: no download, straight
    to extraction (manual and scan-default agree on this happy path)."""

    async def _no_deberia_llamarse(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("no debería descargarse: el texto ya está persistido")

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _no_deberia_llamarse)
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="ok", texto_extraido=TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()

    obligaciones, avisos = await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)

    assert (obligaciones, avisos) == extraccion["resultado"]


async def test_manual_ignora_sin_texto_memoizado_y_reintenta_descarga(
    db: AsyncSession, contrato: Contrato, extraccion: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `texto_estado='sin_texto'` memoized by a PRIOR scan must NOT block a
    fresh manual attempt (this is the bug: a stale scan verdict blocking the
    user's deliberate action) -- the manual path re-downloads and succeeds."""

    async def _fake_descarga(url: str, timeout: float = 0.0) -> bytes:
        assert timeout == checklist_service._VINCULAR_HTTP_TIMEOUT
        return b"contenido"

    from app.services import document_service

    async def _texto_ok(content: bytes, filename: str, *, relaxed_ocr: bool = False) -> tuple[str | None, list[str]]:
        return TEXTO_CONTRATO, []

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_descarga)
    monkeypatch.setattr(document_service, "extraer_texto_documento", _texto_ok)
    doc = _secop_doc(
        contrato,
        "contrato.docx",
        texto_estado="sin_texto",
        texto_extraido=None,
        url_descarga="https://s/contrato.docx",
    )
    db.add(doc)
    await db.commit()

    obligaciones, avisos = await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)

    assert (obligaciones, avisos) == extraccion["resultado"]
    assert doc.texto_estado == "ok"
    assert doc.texto_extraido == TEXTO_CONTRATO


async def test_manual_sin_url_descarga_lanza_mensaje_especifico(db: AsyncSession, contrato: Contrato) -> None:
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="error", texto_extraido=None, url_descarga=None)
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError, match="URL de descarga"):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)


async def test_manual_timeout_de_descarga_lanza_mensaje_especifico(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_descarga(url: str, timeout: float = 0.0) -> bytes:
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_descarga)
    doc = _secop_doc(contrato, "contrato.docx", url_descarga="https://s/contrato.docx")
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError, match="tiempo límite"):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)


async def test_manual_error_http_lanza_mensaje_con_status(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_descarga(url: str, timeout: float = 0.0) -> bytes:
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_descarga)
    doc = _secop_doc(contrato, "contrato.docx", url_descarga="https://s/contrato.docx")
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError, match="HTTP 403"):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)


async def test_manual_documento_demasiado_grande_lanza_mensaje_especifico(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_descarga(url: str, timeout: float = 0.0) -> bytes:
        raise ValueError("documento excede 26214400 bytes")

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_descarga)
    doc = _secop_doc(contrato, "contrato.docx", url_descarga="https://s/contrato.docx")
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError, match="25 MB"):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)


async def test_manual_texto_no_extraible_ni_por_vision_lanza_mensaje_final(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native + OCR + vision all fail to recover text: raises with the new
    terminal copy (NOT the old 'subí el documento manualmente' message)."""
    from app.services import document_service

    async def _fake_descarga(url: str, timeout: float = 0.0) -> bytes:
        return b"%PDF-escaneado-sin-texto"

    async def _sin_texto(content: bytes, filename: str, *, relaxed_ocr: bool = False) -> tuple[str | None, list[str]]:
        return None, []

    async def _vision_falla(content: bytes, mime_type: str) -> None:
        return None

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_descarga)
    monkeypatch.setattr(document_service, "extraer_texto_documento", _sin_texto)
    monkeypatch.setattr(document_service, "_extraer_contrato_multimodal", _vision_falla)
    doc = _secop_doc(contrato, "contrato.pdf", url_descarga="https://s/contrato.pdf")
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError, match="modelo de visión"):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)

    assert doc.texto_estado == "sin_texto"


async def test_manual_error_inesperado_lanza_mensaje_generico_y_loguea(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_descarga(url: str, timeout: float = 0.0) -> bytes:
        raise RuntimeError("boom")

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake_descarga)
    doc = _secop_doc(contrato, "contrato.docx", url_descarga="https://s/contrato.docx")
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc, manual=True)
