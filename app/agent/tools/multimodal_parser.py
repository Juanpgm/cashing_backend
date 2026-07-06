"""Multimodal document helpers — pure functions for the hybrid text→vision path.

These functions decide when text extraction is good enough and, when it is not
(scanned PDF or image), build the multimodal content part so a vision-capable
model can read the file directly. The vision model acts as the OCR — no separate
OCR pipeline is required.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from app.core.file_validation import ALLOWED_MIME_TYPES

# extension → MIME, derived from the single source of truth in file_validation.
_EXT_TO_MIME: dict[str, str] = {ext: mime for mime, exts in ALLOWED_MIME_TYPES.items() for ext in exts}

# MIME types a vision model can read directly as an image/file content part.
MULTIMODAL_MIME_TYPES: frozenset[str] = frozenset({"application/pdf", "image/jpeg", "image/png"})


def guess_mime_type(filename: str) -> str:
    """Map a filename extension to its MIME type (octet-stream when unknown)."""
    ext = Path(filename).suffix.lower()
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


def is_multimodal_supported(mime_type: str) -> bool:
    """Return True when the vision path can read this MIME type directly."""
    return mime_type in MULTIMODAL_MIME_TYPES


def is_text_sufficient(texto: str | None, min_chars: int = 200) -> bool:
    """Return True when extracted text is rich enough to skip the vision path.

    Checks two conditions:
    1. Enough characters (> min_chars) — rules out blank scans.
    2. Reasonable word spacing — fewer than 5 % of tokens exceed 20 characters.
       OCR engines optimised for CJK (e.g. RapidOCR) often concatenate Spanish
       words into long runs ("OBLIGACIONESESPECIFICAS", "CLAUSULACUARTA…").
       Spanish text rarely has > 1-2 % of words over 20 chars; above 5 % the
       text is considered concatenated and the caller should escalate to vision.
    """
    if not texto:
        return False
    stripped = texto.strip()
    if len(stripped) < min_chars:
        return False
    words = stripped.split()
    if not words:
        return False
    long_word_ratio = sum(1 for w in words if len(w) > 20) / len(words)
    return long_word_ratio <= 0.05


# Phone/tablet photos arrive rotated (EXIF orientation) and very large (10+ MP),
# which both hurts vision accuracy and can exceed a provider's request-size limit.
# Normalising the longest side to this many pixels keeps the base64 payload small
# while staying legible for the model.
MAX_IMAGE_DIMENSION: int = 2200


def normalize_image(content: bytes, mime_type: str, *, max_dimension: int = MAX_IMAGE_DIMENSION) -> bytes:
    """Return image bytes corrected for EXIF rotation and downscaled if oversized.

    Phone/tablet cameras embed an orientation flag and produce huge images; vision
    models read an upright, reasonably sized image best. Non-image MIME types and
    any failure return the original bytes unchanged (never block the upload).
    """
    if mime_type not in {"image/jpeg", "image/png"}:
        return content
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(content)) as img:
            img = ImageOps.exif_transpose(img)  # honour the camera orientation flag
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension))
            out = io.BytesIO()
            if mime_type == "image/png":
                img.save(out, format="PNG")
            else:
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                img.save(out, format="JPEG", quality=90)
            return out.getvalue()
    except Exception:
        return content


def build_file_content_part(content: bytes, mime_type: str) -> dict[str, Any]:
    """Build a LiteLLM multimodal content part (base64 data URL) for the file.

    Images use the ``image_url`` part; PDFs use the ``file`` part with inline
    ``file_data`` so the model receives the document directly.
    """
    b64 = base64.b64encode(content).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    if mime_type == "application/pdf":
        return {"type": "file", "file": {"file_data": data_url, "format": mime_type}}
    return {"type": "image_url", "image_url": {"url": data_url}}


# Model prefixes whose providers can ingest a PDF file part natively. Everything
# else (e.g. local Ollama vision models) only accepts images, so PDFs must be
# rasterized first.
NATIVE_PDF_MODEL_PREFIXES: tuple[str, ...] = ("gemini/", "vertex_ai/", "openai/", "gpt-", "anthropic/", "claude")

# Per-provider hard limits on the number of image parts accepted in a single request.
_PROVIDER_MAX_IMAGES: dict[str, int] = {
    "groq/": 5,
}


def supports_native_pdf(model: str | None) -> bool:
    """Return True when the model's provider can read a PDF file part directly.

    Cloud providers (Gemini, OpenAI, Anthropic) read PDFs natively; local Ollama
    vision models only accept images. When ``model`` is None we assume a cloud
    default (native PDF support).
    """
    if not model:
        return True
    return model.startswith(NATIVE_PDF_MODEL_PREFIXES)


def rasterize_pdf(content: bytes, max_pages: int, dpi: int) -> list[bytes]:
    """Render the first ``max_pages`` PDF pages to PNG bytes via PyMuPDF.

    PyMuPDF ships as a pure wheel (no system dependencies such as poppler), so
    this works on a Windows dev machine out of the box. Shared by the vision
    content builder and the local OCR tier.
    """
    import fitz  # PyMuPDF

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    images: list[bytes] = []
    with fitz.open(stream=content, filetype="pdf") as doc:
        for index in range(min(len(doc), max_pages)):
            pix = doc[index].get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
    return images


def _provider_max_pages(model: str | None, requested: int) -> int:
    """Cap ``requested`` pages to the provider's image-count limit (if any)."""
    if not model:
        return requested
    for prefix, limit in _PROVIDER_MAX_IMAGES.items():
        if model.startswith(prefix):
            return min(requested, limit)
    return requested


def build_multimodal_content_parts(
    content: bytes,
    mime_type: str,
    model: str | None = None,
    *,
    max_pdf_pages: int = 8,
    dpi: int = 150,
) -> list[dict[str, Any]]:
    """Build the multimodal content part(s) to attach to an LLM message.

    Images become a single ``image_url`` part. PDFs become a single ``file`` part
    for providers that read PDFs natively (Gemini/OpenAI/Anthropic), or one
    rasterized ``image_url`` part per page for local vision models that only
    accept images. The page count is capped to the provider's image limit.
    """
    if mime_type == "application/pdf" and not supports_native_pdf(model):
        effective_pages = _provider_max_pages(model, max_pdf_pages)
        pages = rasterize_pdf(content, effective_pages, dpi)
        if pages:
            return [build_file_content_part(png, "image/png") for png in pages]
    if mime_type in {"image/jpeg", "image/png"}:
        # Correct rotation and downscale phone/tablet photos before sending.
        content = normalize_image(content, mime_type)
    return [build_file_content_part(content, mime_type)]
