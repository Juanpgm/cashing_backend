"""Tests for direct document download (GET /documentos/{id}/archivo).

Streams the stored bytes through the backend (works for local + prod storage),
unlike /descargar which returns a presigned URL that does not resolve locally.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"


async def _doc(db: AsyncSession, usuario_id: Any, *, nombre: str = "informe.docx") -> DocumentoFuente:
    d = DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=None,
        storage_key=f"k/{nombre}",
        nombre=nombre,
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


async def test_descargar_archivo_streams_bytes(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession
) -> None:
    doc = await _doc(db, test_user["user"].id)
    fake = AsyncMock()
    fake.download = AsyncMock(return_value=b"DOCX-BYTES-CONTENT")
    with patch(_PATCH_S3, return_value=fake):
        r = await client.get(f"/api/v1/documentos/{doc.id}/archivo", headers=test_user["headers"])
    assert r.status_code == 200, r.text
    assert r.content == b"DOCX-BYTES-CONTENT"
    assert "informe.docx" in r.headers["content-disposition"]
    fake.download.assert_awaited_once_with(doc.storage_key)


async def test_descargar_archivo_inexistente_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    r = await client.get(f"/api/v1/documentos/{uuid.uuid4()}/archivo", headers=test_user["headers"])
    assert r.status_code == 404, r.text
