"""Upload validation errors must be readable by the people who see them.

The 2026-09-04 production report was a screenshot of the UI showing
`File type not allowed: 10.CERTIFICADO DEPENDIENTE ....pdf` verbatim: the
frontend renders `detail` as-is, and these strings were English developer text.
They are user-facing copy in a Spanish-only product, and the "not allowed" one
never said WHICH formats are allowed.

Only the upload validation path is covered here; error `code`s are unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.file_validation import MAX_FILE_SIZE_BYTES
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_PDF_MAGIC = b"%PDF-1.4\n"
_UPLOAD = "/api/v1/documentos/upload"
_BATCH = "/api/v1/documentos/upload-batch"


class TestMensajesDeValidacionEnEspanol:
    async def test_extension_no_permitida_nombra_los_formatos_aceptados(
        self, client: AsyncClient, test_user: dict[str, Any]
    ) -> None:
        r = await client.post(
            _UPLOAD,
            headers=test_user["headers"],
            files={"file": ("virus.exe", b"MZ\x90\x00", "application/octet-stream")},
        )

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "Tipo de archivo no permitido" in detalle, detalle
        assert "virus.exe" in detalle, detalle
        assert "PDF" in detalle and "DOCX" in detalle, f"the accepted formats must be listed: {detalle}"

    async def test_archivo_vacio(self, client: AsyncClient, test_user: dict[str, Any]) -> None:
        r = await client.post(
            _UPLOAD,
            headers=test_user["headers"],
            files={"file": ("vacio.pdf", b"", "application/pdf")},
        )

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "está vacío" in detalle, detalle
        assert "vacio.pdf" in detalle, detalle

    async def test_archivo_demasiado_grande(self, client: AsyncClient, test_user: dict[str, Any]) -> None:
        contenido = _PDF_MAGIC + b"0" * (MAX_FILE_SIZE_BYTES + 1 - len(_PDF_MAGIC))

        r = await client.post(
            _UPLOAD,
            headers=test_user["headers"],
            files={"file": ("enorme.pdf", contenido, "application/pdf")},
        )

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "supera el máximo" in detalle, detalle
        assert "10 MB" in detalle, detalle

    async def test_contenido_que_no_coincide_con_la_extension(
        self, client: AsyncClient, test_user: dict[str, Any]
    ) -> None:
        r = await client.post(
            _UPLOAD,
            headers=test_user["headers"],
            files={"file": ("disfrazado.pdf", b"not a pdf at all" * 20, "application/pdf")},
        )

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "no coincide con su extensión" in detalle, detalle
        assert "disfrazado.pdf" in detalle, detalle

    async def test_batch_usa_los_mismos_mensajes(self, client: AsyncClient, test_user: dict[str, Any]) -> None:
        """The batch pre-validation must not drift back into English."""
        r = await client.post(
            _BATCH,
            headers=test_user["headers"],
            files=[
                ("files", ("bueno.pdf", _PDF_MAGIC + b"x" * 512, "application/pdf")),
                ("files", ("virus.exe", b"MZ\x90\x00", "application/octet-stream")),
            ],
        )

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "Tipo de archivo no permitido" in detalle, detalle
        assert "virus.exe" in detalle, detalle
