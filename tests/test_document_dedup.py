"""Tests for content-hash dedup (B4, migración 039) in document_service.upload_document.

Same content under a different filename must reuse the existing DocumentoFuente
row (no new row, no new storage object). Different content under the same
filename keeps prior behavior unchanged. tipo=CONTRATO replace still deletes
and replaces the prior CONTRATO document.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.services.document_service import upload_document
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"

# 200+ chars so is_text_sufficient passes without needing OCR/vision mocks.
_TEXTO_SUFICIENTE = "Contenido de prueba para verificar deduplicación por hash. " * 5


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-DEDUP-001",
        objeto="Objeto de prueba",
        valor_total=1_000_000.0,
        valor_mensual=100_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato.id


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


class TestContentHashDedup:
    @pytest.mark.asyncio
    async def test_same_bytes_different_filename_returns_existing(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(a) Same content, different filename → existing doc returned, no new row/upload."""
        user = test_user["user"]
        contrato_id = await _crear_contrato(db, user.id)
        content = _TEXTO_SUFICIENTE.encode()

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage = _mock_storage()
            mock_storage_cls.return_value = mock_storage

            first = await upload_document(
                db=db,
                user_id=user.id,
                filename="original.txt",
                content=content,
                content_type="text/plain",
                tipo=TipoDocumentoFuente.RUT,
                contrato_id=contrato_id,
            )
            second = await upload_document(
                db=db,
                user_id=user.id,
                filename="renombrado.txt",
                content=content,
                content_type="text/plain",
                tipo=TipoDocumentoFuente.RUT,
                contrato_id=contrato_id,
            )

        assert second.id == first.id
        assert mock_storage.upload.call_count == 1  # only the first upload hit storage

        count = await db.execute(
            select(func.count()).select_from(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato_id)
        )
        assert count.scalar_one() == 1

    @pytest.mark.asyncio
    async def test_different_bytes_same_filename_preserves_current_behavior(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(b) Different content, same filename → dedups by name as before (existing returned)."""
        user = test_user["user"]
        contrato_id = await _crear_contrato(db, user.id)

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage = _mock_storage()
            mock_storage_cls.return_value = mock_storage

            first = await upload_document(
                db=db,
                user_id=user.id,
                filename="mismo_nombre.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.RUT,
                contrato_id=contrato_id,
            )
            second = await upload_document(
                db=db,
                user_id=user.id,
                filename="mismo_nombre.txt",
                content=(_TEXTO_SUFICIENTE + "otro contenido distinto").encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.RUT,
                contrato_id=contrato_id,
            )

        # Existing (pre-B4) exact-filename dedup behavior: unchanged, existing row returned.
        assert second.id == first.id
        assert mock_storage.upload.call_count == 1

        count = await db.execute(
            select(func.count()).select_from(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato_id)
        )
        assert count.scalar_one() == 1

    @pytest.mark.asyncio
    async def test_contrato_replace_path_still_deletes_and_replaces(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(c) tipo=CONTRATO still deletes+replaces the prior CONTRATO doc (unchanged)."""
        user = test_user["user"]
        contrato_id = await _crear_contrato(db, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage = _mock_storage()
            mock_storage_cls.return_value = mock_storage

            first = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato_v1.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.CONTRATO,
                contrato_id=contrato_id,
            )
            second = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato_v2.txt",
                content=(_TEXTO_SUFICIENTE + "version 2").encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.CONTRATO,
                contrato_id=contrato_id,
            )

        assert second.id != first.id
        mock_storage.delete.assert_called_once()

        rows = await db.execute(
            select(DocumentoFuente).where(
                DocumentoFuente.contrato_id == contrato_id,
                DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
            )
        )
        remaining = rows.scalars().all()
        assert len(remaining) == 1
        assert remaining[0].id == second.id

    @pytest.mark.asyncio
    async def test_sha256_populated_on_new_insert(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        """(d) sha256 is populated on new DocumentoFuente inserts."""
        user = test_user["user"]
        contrato_id = await _crear_contrato(db, user.id)
        content = _TEXTO_SUFICIENTE.encode()
        expected_hash = hashlib.sha256(content).hexdigest()

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage = _mock_storage()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="con_hash.txt",
                content=content,
                content_type="text/plain",
                tipo=TipoDocumentoFuente.RUT,
                contrato_id=contrato_id,
            )

        row = await db.get(DocumentoFuente, result.id)
        assert row is not None
        assert row.sha256 == expected_hash
