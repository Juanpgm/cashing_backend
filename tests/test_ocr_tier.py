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


def test_extract_text_downscales_oversized_image_before_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standalone image (e.g. a 10+ MP phone photo) must be downscaled via
    `normalize_image` BEFORE it reaches the OCR engine -- mirrors the vision
    path's downscaling so one oversized image can't stall the shared,
    lock-serialized OCR engine."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4000, 4000), "white").save(buf, format="PNG")
    big_png = buf.getvalue()

    captured: dict[str, Any] = {}

    def _stub(image_bytes: bytes, lang: str) -> str:
        with Image.open(io.BytesIO(image_bytes)) as img:
            captured["size"] = img.size
        return "texto ocr"

    monkeypatch.setattr(ocr, "_ocr_image_tesseract", _stub)
    text = ocr.extract_text(big_png, "image/png", engine="tesseract", lang="spa", max_pages=1, dpi=72)

    assert text == "texto ocr"
    assert max(captured["size"]) <= 2200


def test_rapidocr_engine_built_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """RapidOCR() loads 3 ONNX models (expensive); OCRing a multi-page PDF must
    construct it ONCE and reuse it for every page, and a later call must reuse
    that same shared instance rather than rebuilding it."""
    import fitz  # PyMuPDF
    import rapidocr_onnxruntime

    ocr._engine = None

    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc.new_page()
    pdf_bytes = doc.tobytes()
    doc.close()

    construct_calls = {"n": 0}

    class _FakeEngine:
        def __init__(self) -> None:
            construct_calls["n"] += 1

        def __call__(self, arr: Any) -> tuple[list[list[Any]], None]:
            return ([[None, "texto ocr"]], None)

    monkeypatch.setattr(rapidocr_onnxruntime, "RapidOCR", _FakeEngine)

    try:
        text = ocr.extract_text(pdf_bytes, "application/pdf", engine="rapidocr", lang="spa", max_pages=8, dpi=72)
        assert construct_calls["n"] == 1
        assert "texto ocr" in text

        # A second extraction (e.g. the next document) reuses the cached engine.
        ocr.extract_text(pdf_bytes, "application/pdf", engine="rapidocr", lang="spa", max_pages=8, dpi=72)
        assert construct_calls["n"] == 1
    finally:
        ocr._engine = None


def test_rapidocr_inference_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared engine mutates per-call state, so inference must be serialized:
    two threads must never be inside engine(arr) at once. Guards against a future
    removal of _engine_lock."""
    import io
    import threading
    import time

    import rapidocr_onnxruntime
    from PIL import Image

    ocr._engine = None

    counter = {"now": 0, "max": 0}
    guard = threading.Lock()

    class _CountingEngine:
        def __init__(self) -> None: ...

        def __call__(self, arr: Any) -> tuple[list[list[Any]], None]:
            with guard:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            time.sleep(0.02)  # widen the overlap window so a missing lock would show
            with guard:
                counter["now"] -= 1
            return ([[None, "ocr"]], None)

    monkeypatch.setattr(rapidocr_onnxruntime, "RapidOCR", _CountingEngine)

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
    png = buf.getvalue()

    barrier = threading.Barrier(4)

    def _run() -> None:
        barrier.wait()  # release all threads together to force contention
        ocr._ocr_image_rapidocr(png)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert counter["max"] == 1  # _engine_lock kept them one-at-a-time
    finally:
        ocr._engine = None


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
