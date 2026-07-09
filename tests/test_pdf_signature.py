"""PDF PAdES signature tests — service signing + constancia endpoint integration.

Uses a pyhanko-built blank PDF as the input to sign (WeasyPrint is not exercised
here; the constancia generator is mocked in the integration tests).
"""

from __future__ import annotations

import io
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import pdf_signature_service


def _blank_pdf() -> bytes:
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.writer import PdfFileWriter

    w = PdfFileWriter()
    page = generic.DictionaryObject(
        {
            generic.pdf_name("/Type"): generic.pdf_name("/Page"),
            generic.pdf_name("/MediaBox"): generic.ArrayObject(
                list(map(generic.NumberObject, (0, 0, 595, 842)))
            ),
            generic.pdf_name("/Resources"): generic.DictionaryObject(),
        }
    )
    w.insert_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _reset_signer():
    """Drop the process-cached signer so config changes take effect per test."""
    pdf_signature_service._signer = None
    yield
    pdf_signature_service._signer = None


@pytest.mark.asyncio
async def test_firma_activa_reflects_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pdf_signature_service.firma_activa() is False  # default
    monkeypatch.setattr(settings, "PDF_SIGNATURE_ENABLED", True)
    assert pdf_signature_service.firma_activa() is True


@pytest.mark.asyncio
async def test_firmar_pdf_produces_pades_signature() -> None:
    original = _blank_pdf()
    signed = await pdf_signature_service.firmar_pdf(original)

    assert len(signed) > len(original)
    assert b"ByteRange" in signed
    assert b"/Sig" in signed
    # Still a valid PDF document
    assert signed[:5] == b"%PDF-"


@pytest.mark.asyncio
async def test_constancia_endpoint_signs_when_enabled(
    client: AsyncClient, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PDF_SIGNATURE_ENABLED", True)
    cuenta_id = uuid.uuid4()
    mock_gen = AsyncMock(return_value=(_blank_pdf(), "constancia.pdf"))

    with patch("app.services.constancia_service.generar_constancia_pdf", new=mock_gen):
        resp = await client.get(
            f"/api/v1/cuentas-cobro/{cuenta_id}/constancia.pdf",
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert b"ByteRange" in resp.content  # signed


@pytest.mark.asyncio
async def test_constancia_endpoint_unsigned_when_disabled(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    assert settings.PDF_SIGNATURE_ENABLED is False  # default
    cuenta_id = uuid.uuid4()
    unsigned = _blank_pdf()
    mock_gen = AsyncMock(return_value=(unsigned, "constancia.pdf"))

    with patch("app.services.constancia_service.generar_constancia_pdf", new=mock_gen):
        resp = await client.get(
            f"/api/v1/cuentas-cobro/{cuenta_id}/constancia.pdf",
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    assert resp.content == unsigned  # returned as-is, no signature
    assert b"ByteRange" not in resp.content
