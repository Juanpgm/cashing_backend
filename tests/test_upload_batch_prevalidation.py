"""Regression: upload-batch validated files as it went, committing earlier files
before a later file's validation error was raised.

`POST /documentos/upload-batch` (app/api/v1/documentos.py) looped over files and
called `document_service.upload_document` (which persists) for each one BEFORE
validating the next file's extension/size/MIME. A batch of [valid.pdf, bad.exe]
persisted `valid.pdf` to the DB, then raised 422 for `bad.exe` and discarded the
whole response — the client sees a 422 with no indication a document was actually
written, and a retry of the same batch duplicates `valid.pdf`.

Also: a 0-byte file failed `validate_file_size` (which returns False for size <= 0
and for size > MAX) with the misleading message "exceeds maximum size of 10 MB".
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"
_PDF_MAGIC = b"%PDF-1.4\n"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_cuenta(db: AsyncSession, user_id: uuid.UUID) -> CuentaCobro:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-BATCH-001",
        objeto="Objeto de prueba",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2025,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)

    # Seed the checklist rows the real cuenta-creation flow always creates (A3:
    # upload_document now propagates a failed checklist link instead of
    # swallowing it).
    from app.services import checklist_service

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    return cuenta


class TestBatchPreValidatesBeforePersisting:
    async def test_mixed_valid_and_invalid_batch_persists_nothing(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files=[
                    ("files", ("valido.pdf", _PDF_MAGIC + b"x" * 2048, "application/pdf")),
                    ("files", ("malicioso.exe", b"MZ" + b"x" * 2048, "application/octet-stream")),
                ],
            )

        assert r.status_code == 422, r.text

        docs = (
            await db.execute(select(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta.id))
        ).scalars().all()
        assert not docs, (
            f"validating file 2 must not have persisted file 1 first, but found: "
            f"{[d.nombre for d in docs]}"
        )

    async def test_empty_file_gets_a_clear_empty_file_message(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files=[("files", ("vacio.pdf", b"", "application/pdf"))],
            )

        assert r.status_code == 422, r.text
        assert "supera el máximo" not in r.text, "a 0-byte file is not an oversized file"
        assert "está vacío" in r.json()["detail"], r.text

    async def test_valid_batch_still_persists_all_files(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files=[
                    ("files", ("uno.pdf", _PDF_MAGIC + b"x" * 2048, "application/pdf")),
                    ("files", ("dos.pdf", _PDF_MAGIC + b"y" * 2048, "application/pdf")),
                ],
            )

        assert r.status_code == 201, r.text
        assert len(r.json()) == 2


class TestSingleUploadEmptyFileMessage:
    """The single-file endpoint shares the same misleading message for a 0-byte
    file (`validate_file_size` returns False for size <= 0 AND size > MAX)."""

    async def test_single_upload_empty_file_gets_a_clear_message(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"file": ("vacio.pdf", b"", "application/pdf")},
            )

        assert r.status_code == 422, r.text
        assert "supera el máximo" not in r.text, "a 0-byte file is not an oversized file"
        assert "está vacío" in r.json()["detail"], r.text
