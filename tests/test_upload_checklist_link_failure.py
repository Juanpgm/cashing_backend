"""Regression: a failed checklist link silently returned 201.

`upload_document` (app/services/document_service.py:1160-1178) called
`checklist_service.vincular_documento_fuente` inside a bare `try/except Exception`
that only logged a warning and returned the normal success response anyway — the
document was persisted, but the UI showed green while the requisito stayed
Pendiente, with no signal that anything had gone wrong.

Also: the content-hash dedup fast path (~895-909) returned the existing document
without EVER attempting to link it — so re-uploading the same file ("Reintentar"
in the UI) could never repair a requisito that failed to link the first time.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import DomainError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.documento_fuente import TipoDocumentoFuente
from app.services.document_service import upload_document
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"

# 200+ chars so is_text_sufficient passes without needing OCR/vision mocks.
_TEXTO_SUFICIENTE = "Contenido de prueba para verificar el vinculo de checklist. " * 5


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_cuenta(db: AsyncSession, user_id: uuid.UUID) -> CuentaCobro:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-LINKFAIL-001",
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
    return cuenta


class TestChecklistLinkFailureIsSurfaced:
    async def test_missing_requisito_row_raises_instead_of_returning_201(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """No DocumentoCuentaCobro row exists for 'EVIDENCIAS' — the link must fail
        loudly (NotFoundError, a DomainError) instead of a silently-swallowed 201."""
        user = test_user["user"]
        cuenta = await _crear_cuenta(db, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            with pytest.raises(DomainError):
                await upload_document(
                    db=db,
                    user_id=user.id,
                    filename="evidencia.pdf",
                    content=_TEXTO_SUFICIENTE.encode(),
                    content_type="text/plain",
                    tipo=TipoDocumentoFuente.OTROS,
                    contrato_id=cuenta.contrato_id,
                    cuenta_cobro_id=cuenta.id,
                    requisito_codigo="EVIDENCIAS",
                )

    async def test_document_stays_persisted_after_link_failure(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """The document itself must NOT be rolled back just because the downstream
        checklist link failed — losing the upload would be worse than losing the
        link."""
        from app.models.documento_fuente import DocumentoFuente

        user = test_user["user"]
        cuenta = await _crear_cuenta(db, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            with pytest.raises(DomainError):
                await upload_document(
                    db=db,
                    user_id=user.id,
                    filename="evidencia.pdf",
                    content=_TEXTO_SUFICIENTE.encode(),
                    content_type="text/plain",
                    tipo=TipoDocumentoFuente.OTROS,
                    contrato_id=cuenta.contrato_id,
                    cuenta_cobro_id=cuenta.id,
                    requisito_codigo="EVIDENCIAS",
                )

        docs = (await db.execute(DocumentoFuente.__table__.select())).fetchall()
        assert len(docs) == 1, "the document must remain persisted despite the link failure"


class TestRetryRepairsTheLink:
    async def test_reuploading_same_content_repairs_a_previously_failed_link(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        cuenta = await _crear_cuenta(db, user.id)
        content = _TEXTO_SUFICIENTE.encode()

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()

            # First upload: no checklist row yet -> link fails, document persists.
            with pytest.raises(DomainError):
                await upload_document(
                    db=db,
                    user_id=user.id,
                    filename="evidencia.pdf",
                    content=content,
                    content_type="text/plain",
                    tipo=TipoDocumentoFuente.OTROS,
                    contrato_id=cuenta.contrato_id,
                    cuenta_cobro_id=cuenta.id,
                    requisito_codigo="EVIDENCIAS",
                )

            # Simulate the requisito row now existing (e.g. checklist was generated
            # after the first failed attempt) — same shape "Reintentar" would hit.
            db.add(DocumentoCuentaCobro(cuenta_cobro_id=cuenta.id, requisito_codigo="EVIDENCIAS"))
            await db.commit()

            # Retry: same content (content-hash dedup fast path) must re-attempt
            # the link instead of returning early without linking.
            await upload_document(
                db=db,
                user_id=user.id,
                filename="evidencia.pdf",
                content=content,
                content_type="text/plain",
                tipo=TipoDocumentoFuente.OTROS,
                contrato_id=cuenta.contrato_id,
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="EVIDENCIAS",
            )

        fila = (
            await db.execute(
                DocumentoCuentaCobro.__table__.select().where(
                    DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
                    DocumentoCuentaCobro.requisito_codigo == "EVIDENCIAS",
                )
            )
        ).fetchone()
        assert fila is not None
        assert fila.estado == EstadoRequisito.CARGADO, "retry must have linked the document"
        assert fila.documento_fuente_id is not None
