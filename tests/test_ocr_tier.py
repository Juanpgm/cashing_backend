"""OCR tier: módulo de OCR local y su lugar en la escalera de extracción.

La escalera para escaneados es: texto → OCR (determinístico) → visión (último).
Aquí se prueba el módulo OCR de forma aislada y que, cuando el OCR recupera
texto suficiente, las obligaciones salen del extractor determinístico SIN llamar
al modelo de visión.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.agent.tools import ocr
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion
from app.services import document_service as ds
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ── Módulo OCR (aislado) ────────────────────────────────────────────────────


def test_ocr_available_false_for_unknown_engine() -> None:
    assert ocr.ocr_available("bogus") is False


def test_ocr_available_returns_bool_for_tesseract() -> None:
    # No asumimos que el binario esté instalado: solo que degrada a bool, sin crashear.
    assert isinstance(ocr.ocr_available("tesseract"), bool)


def test_extract_text_rasterizes_and_ocrs_each_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un PDF se rasteriza a una imagen por página y cada una pasa por el motor."""
    import fitz  # PyMuPDF

    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    pdf_bytes = doc.tobytes()
    doc.close()

    calls = {"n": 0}

    def _stub(image_bytes: bytes, lang: str) -> str:
        calls["n"] += 1
        return f"texto de la pagina {calls['n']}"

    monkeypatch.setattr(ocr, "_ocr_image_tesseract", _stub)
    text = ocr.extract_text(pdf_bytes, "application/pdf", engine="tesseract", lang="spa", max_pages=8, dpi=72)

    assert calls["n"] == 2
    assert "pagina 1" in text
    assert "pagina 2" in text


def test_extract_text_unsupported_mime_is_empty() -> None:
    assert ocr.extract_text(b"x", "text/plain", engine="tesseract", lang="spa", max_pages=1, dpi=72) == ""


# ── Escalera: OCR exitoso NO llama a visión ─────────────────────────────────


class _FakeStorage:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    async def download(self, key: str) -> bytes:
        return b"%PDF-1.4 scanned"


@pytest.mark.asyncio
async def test_reextraccion_usa_ocr_y_evita_vision(
    db: AsyncSession,
    test_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = test_user["user"]
    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-OCR-001",
        objeto="Restauración",
        valor_total=10_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    db.add(
        DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            storage_key=f"usuarios/{user.id}/escaneado.pdf",
            nombre="escaneado.pdf",
            tipo=TipoDocumentoFuente.CONTRATO,
            texto_extraido="",
        )
    )
    await db.commit()

    texto_ocr = (
        "CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
        "1. Realizar el diagnóstico ambiental del predio priorizado.\n"
        "2. Las demás que asigne la supervisión relacionadas con el objeto del contrato.\n"
        "VALOR DEL CONTRATO: diez millones."
    )

    def _vision_must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("La visión NO debe llamarse cuando el OCR recupera texto suficiente")

    monkeypatch.setattr(ds, "ocr_available", lambda engine: True)
    monkeypatch.setattr(ds, "ocr_extract_text", lambda *a, **k: texto_ocr)
    monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
    monkeypatch.setattr(ds, "_extraer_contrato_multimodal", _vision_must_not_run)
    monkeypatch.setattr(ds.settings, "EXTRACTION_OCR_ENABLED", True)
    monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", True)

    obligaciones, _avisos = await ds.extraer_obligaciones_documento(contrato_id=contrato.id, user_id=user.id, db=db)

    assert len(obligaciones) == 2
    assert obligaciones[0].descripcion.startswith("Realizar el diagnóstico ambiental")

    rows = (await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato.id))).scalars().all()
    assert len(rows) == 2
