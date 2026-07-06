"""Local OCR tier for scanned documents (free, fast, no LLM).

Runs before the vision model in the extraction escalation: rasterizes a PDF (or
reads an image) and recovers text with a local OCR engine. The deterministic
obligation extractor then runs on that text exactly as for a native-text PDF.

Engines:
  - ``tesseract`` (default): the ``pytesseract`` wrapper over the Tesseract OCR
    binary. Pure-Python wrapper (works on any CPython), but needs the Tesseract
    binary installed plus the Spanish language data (``spa``).
  - ``rapidocr``: ``rapidocr_onnxruntime`` — pip-only (no system binary), but
    depends on ``onnxruntime`` which lacks wheels for the newest CPython.

``ocr_available`` gates every call: when the engine cannot run the caller skips
the OCR tier and escalates to the vision model, so nothing breaks if OCR is not
installed.
"""

from __future__ import annotations

import io

import structlog

from app.agent.tools.multimodal_parser import rasterize_pdf
from app.core.config import settings

logger = structlog.get_logger("agent.tools.ocr")

_IMAGE_MIMES = frozenset({"image/png", "image/jpeg"})


def _configure_tesseract() -> None:
    """Point pytesseract at TESSERACT_CMD when the binary is not on PATH (Windows)."""
    if settings.TESSERACT_CMD:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD


def ocr_available(engine: str) -> bool:
    """Return True when the configured OCR engine can actually run right now."""
    try:
        if engine == "tesseract":
            import pytesseract

            _configure_tesseract()
            pytesseract.get_tesseract_version()  # raises if the binary is missing
            return True
        if engine == "rapidocr":
            import rapidocr_onnxruntime  # noqa: F401

            return True
    except Exception:
        return False
    return False


def _ocr_image_tesseract(image_bytes: bytes, lang: str) -> str:
    import pytesseract
    from PIL import Image

    _configure_tesseract()
    with Image.open(io.BytesIO(image_bytes)) as img:
        return str(pytesseract.image_to_string(img, lang=lang))


def _ocr_image_rapidocr(image_bytes: bytes) -> str:
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    with Image.open(io.BytesIO(image_bytes)) as img:
        arr = np.array(img.convert("RGB"))
    result, _ = engine(arr)
    if not result:
        return ""
    return "\n".join(str(line[1]) for line in result)


def extract_text(
    content: bytes,
    mime_type: str,
    *,
    engine: str,
    lang: str,
    max_pages: int,
    dpi: int,
) -> str:
    """OCR a PDF or image into plain text using the given local engine.

    PDFs are rasterized to images first (one per page, capped at ``max_pages``).
    Returns an empty string for unsupported MIME types.
    """
    if mime_type == "application/pdf":
        pages = rasterize_pdf(content, max_pages, dpi)
    elif mime_type in _IMAGE_MIMES:
        pages = [content]
    else:
        return ""

    parts: list[str] = []
    for page in pages:
        if engine == "tesseract":
            parts.append(_ocr_image_tesseract(page, lang))
        elif engine == "rapidocr":
            parts.append(_ocr_image_rapidocr(page))
    return "\n\n".join(p.strip() for p in parts if p.strip())
