"""Unit tests for the multimodal_parser pure helpers."""

from __future__ import annotations

import base64
import io

from app.agent.tools.multimodal_parser import (
    build_file_content_part,
    guess_mime_type,
    is_multimodal_supported,
    is_text_sufficient,
    normalize_image,
)


class TestIsTextSufficient:
    def test_none_is_insufficient(self) -> None:
        assert is_text_sufficient(None) is False

    def test_empty_is_insufficient(self) -> None:
        assert is_text_sufficient("") is False

    def test_whitespace_only_is_insufficient(self) -> None:
        assert is_text_sufficient("   \n\t  ") is False

    def test_below_threshold_is_insufficient(self) -> None:
        assert is_text_sufficient("x" * 199) is False

    def test_at_threshold_is_sufficient(self) -> None:
        # 200 chars of normal Spanish words (not one 200-char token, which the
        # long-word ratio check correctly flags as OCR-concatenated garbage).
        texto = ("obligacion contractual del contratista " * 6).strip()
        assert len(texto) >= 200
        assert is_text_sufficient(texto) is True

    def test_custom_threshold(self) -> None:
        assert is_text_sufficient("hello", min_chars=3) is True
        assert is_text_sufficient("hi", min_chars=3) is False


class TestNormalizeImage:
    @staticmethod
    def _png(width: int, height: int) -> bytes:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (width, height), "white").save(buf, format="PNG")
        return buf.getvalue()

    def test_non_image_returns_unchanged(self) -> None:
        content = b"%PDF-1.4 not an image"
        assert normalize_image(content, "application/pdf") is content

    def test_corrupt_image_returns_unchanged(self) -> None:
        garbage = b"\x89PNG not really an image"
        assert normalize_image(garbage, "image/png") is garbage

    def test_downscales_oversized_photo(self) -> None:
        from PIL import Image

        big = self._png(5000, 4000)  # ~20 MP, like a phone camera
        out = normalize_image(big, "image/png", max_dimension=2200)
        with Image.open(io.BytesIO(out)) as img:
            assert max(img.size) == 2200

    def test_small_image_not_upscaled(self) -> None:
        from PIL import Image

        small = self._png(800, 600)
        out = normalize_image(small, "image/png", max_dimension=2200)
        with Image.open(io.BytesIO(out)) as img:
            assert img.size == (800, 600)


class TestGuessMimeType:
    def test_pdf(self) -> None:
        assert guess_mime_type("contrato.pdf") == "application/pdf"

    def test_jpg(self) -> None:
        assert guess_mime_type("foto.jpg") == "image/jpeg"
        assert guess_mime_type("foto.jpeg") == "image/jpeg"

    def test_png(self) -> None:
        assert guess_mime_type("imagen.PNG") == "image/png"

    def test_docx(self) -> None:
        assert guess_mime_type("doc.docx") == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def test_unknown(self) -> None:
        assert guess_mime_type("archivo.xyz") == "application/octet-stream"


class TestIsMultimodalSupported:
    def test_supported(self) -> None:
        assert is_multimodal_supported("application/pdf") is True
        assert is_multimodal_supported("image/jpeg") is True
        assert is_multimodal_supported("image/png") is True

    def test_not_supported(self) -> None:
        assert (
            is_multimodal_supported("application/vnd.openxmlformats-officedocument.wordprocessingml.document") is False
        )
        assert is_multimodal_supported("application/octet-stream") is False


class TestBuildFileContentPart:
    def test_pdf_uses_file_part(self) -> None:
        content = b"%PDF-1.4 fake pdf bytes"
        part = build_file_content_part(content, "application/pdf")
        assert part["type"] == "file"
        assert part["file"]["format"] == "application/pdf"
        assert part["file"]["file_data"].startswith("data:application/pdf;base64,")
        # The base64 payload round-trips back to the original bytes.
        b64 = part["file"]["file_data"].split(",", 1)[1]
        assert base64.b64decode(b64) == content

    def test_image_uses_image_url_part(self) -> None:
        content = b"\x89PNG fake image bytes"
        part = build_file_content_part(content, "image/png")
        assert part["type"] == "image_url"
        assert part["image_url"]["url"].startswith("data:image/png;base64,")
        b64 = part["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(b64) == content
