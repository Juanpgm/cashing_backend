"""Guard: a cuenta-scoped upload must never destroy the contract document.

Live data-loss bug: ``upload_document`` enforced the "1 CONTRATO document per
contract" replace rule on ``tipo == CONTRATO and contrato_id is not None``
alone. Because ``contrato_id`` is derived from the cuenta when omitted, and the
``tipo`` query param defaults to ``contrato``, ANY checklist upload that passed
``cuenta_cobro_id`` + ``requisito_codigo`` without an explicit ``tipo`` deleted
the contract's PDF from storage AND its DB row.

These tests pin the invariant from both sides:
  * a cuenta-scoped (or non-CONTRATO requisito) upload never replaces anything;
  * a genuine contract upload still replaces the previous contract document.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.services.document_service import upload_document
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"

# 200+ chars so is_text_sufficient() passes without OCR/vision mocks.
_TEXTO_SUFICIENTE = "Soporte mensual del contratista para el periodo facturado. " * 5

_CONTRATO_STORAGE_KEY = "usuarios/x/documentos/original/contrato-firmado.pdf"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


def _deleted_keys(storage: AsyncMock) -> list[str]:
    return [call.args[0] if call.args else call.kwargs.get("key") for call in storage.delete.call_args_list]


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID, numero: str = "CD-GUARD-001") -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Objeto de prueba para el guard de alcance de documentos",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato


async def _crear_cuenta(db: AsyncSession, contrato: Contrato, mes: int = 1) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
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
    # swallowing it, so tests that upload against a requisito_codigo need the
    # matching DocumentoCuentaCobro row to exist, exactly as production does).
    from app.services import checklist_service

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    return cuenta


async def _crear_documento_contrato(db: AsyncSession, contrato: Contrato, user_id: uuid.UUID) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=user_id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,
        storage_key=_CONTRATO_STORAGE_KEY,
        nombre="contrato-firmado.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
        texto_extraido="Texto del contrato original que no debe perderse.",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _documentos_contrato(db: AsyncSession, contrato_id: uuid.UUID) -> list[DocumentoFuente]:
    res = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
        )
    )
    return list(res.scalars().all())


class TestCuentaScopedUploadNeverDestroysContract:
    async def test_cuenta_level_requisito_upload_keeps_contract_document(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """THE BUG: checklist upload with the default tipo=contrato wiped the contract.

        Drives the exact shape of a checklist upload: no explicit ``tipo`` (so the
        API default ``contrato`` applies), ``cuenta_cobro_id`` set, and a
        cuenta-level ``requisito_codigo``.
        """
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            await upload_document(
                db=db,
                user_id=user.id,
                filename="planilla-mensual.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                # tipo intentionally omitted -> defaults to CONTRATO, as the API does
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="SEGURIDAD_SOCIAL",
            )

        assert _CONTRATO_STORAGE_KEY not in _deleted_keys(storage)
        superviviente = await db.get(DocumentoFuente, doc_contrato.id)
        assert superviviente is not None, "the contract document row was destroyed"
        assert superviviente.storage_key == _CONTRATO_STORAGE_KEY

    async def test_contract_level_requisito_other_than_contrato_keeps_contract_document(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """RUT is contract-level, so it resolves to cuenta_cobro_id=None — the replace
        rule must still not fire, because a RUT is not the contract."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            await upload_document(
                db=db,
                user_id=user.id,
                filename="documento-tributario.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="RUT",
            )

        assert _CONTRATO_STORAGE_KEY not in _deleted_keys(storage)
        assert await db.get(DocumentoFuente, doc_contrato.id) is not None

    async def test_cuenta_scoped_upload_without_requisito_keeps_contract_document(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """cuenta_cobro_id without requisito_codigo is still a cuenta-scoped upload."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            await upload_document(
                db=db,
                user_id=user.id,
                filename="adjunto-suelto.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                cuenta_cobro_id=cuenta.id,
            )

        assert _CONTRATO_STORAGE_KEY not in _deleted_keys(storage)
        assert await db.get(DocumentoFuente, doc_contrato.id) is not None

    async def test_cuenta_scoped_upload_does_not_extract_obligations_onto_contract(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Same root cause one screen further down: obligation extraction was gated on
        the very same `tipo == CONTRATO and contrato_id is not None`, so an arbitrary
        checklist attachment wrote obligations onto the contract.

        Uses a neutral filename on purpose: the A1 content re-classifier only rescues
        this path when it can confidently re-derive the tipo, so it must not be relied
        on as the guard.
        """
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)

        extraer = AsyncMock(return_value=([], []))
        with patch(_PATCH_S3) as mock_storage_cls, patch(_PATCH_OBLIGACIONES, new=extraer):
            mock_storage_cls.return_value = _mock_storage()

            await upload_document(
                db=db,
                user_id=user.id,
                filename="adjunto-generico.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="SEGURIDAD_SOCIAL",
            )

        extraer.assert_not_awaited()

    async def test_several_cuentas_of_one_contract_never_destroy_the_contract(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta_a = await _crear_cuenta(db, contrato, mes=1)
        cuenta_b = await _crear_cuenta(db, contrato, mes=2)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            for i, cuenta in enumerate((cuenta_a, cuenta_b)):
                await upload_document(
                    db=db,
                    user_id=user.id,
                    filename=f"planilla-{i}.txt",
                    content=f"{_TEXTO_SUFICIENTE} mes {i}".encode(),
                    content_type="text/plain",
                    cuenta_cobro_id=cuenta.id,
                    requisito_codigo="SEGURIDAD_SOCIAL",
                )

        assert _CONTRATO_STORAGE_KEY not in _deleted_keys(storage)
        assert await db.get(DocumentoFuente, doc_contrato.id) is not None

    async def test_contract_level_and_cuenta_level_documents_coexist(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            nuevo = await upload_document(
                db=db,
                user_id=user.id,
                filename="planilla-mensual.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="SEGURIDAD_SOCIAL",
            )

        fila_contrato = await db.get(DocumentoFuente, doc_contrato.id)
        fila_cuenta = await db.get(DocumentoFuente, nuevo.id)
        assert fila_contrato is not None and fila_contrato.cuenta_cobro_id is None
        assert fila_cuenta is not None and fila_cuenta.cuenta_cobro_id == cuenta.id

    async def test_upload_against_soft_deleted_contract_is_rejected_and_deletes_nothing(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """A cuenta whose contract was soft-deleted is unreachable — and the failed
        lookup must happen before anything is removed."""
        from app.core.exceptions import NotFoundError

        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        contrato.deleted_at = datetime.now(UTC)
        await db.commit()

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            with pytest.raises(NotFoundError):
                await upload_document(
                    db=db,
                    user_id=user.id,
                    filename="planilla-mensual.txt",
                    content=_TEXTO_SUFICIENTE.encode(),
                    content_type="text/plain",
                    cuenta_cobro_id=cuenta.id,
                    requisito_codigo="SEGURIDAD_SOCIAL",
                )

        storage.delete.assert_not_called()
        assert await db.get(DocumentoFuente, doc_contrato.id) is not None


class TestLegitimateContractReplacementStillWorks:
    async def test_contract_level_upload_replaces_previous_contract_document(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            nuevo = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato-v2.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.CONTRATO,
                contrato_id=contrato.id,
                # B3: upload_document requires an EXPLICIT requisito_codigo=="CONTRATO"
                # to treat this call as the contract (this class's whole point is
                # exercising the legitimate replace path via a direct service call,
                # bypassing the router's Opción A/B default synthesis).
                requisito_codigo="CONTRATO",
            )

        assert _CONTRATO_STORAGE_KEY in _deleted_keys(storage)
        assert await db.get(DocumentoFuente, doc_contrato.id) is None
        restantes = await _documentos_contrato(db, contrato.id)
        assert [d.id for d in restantes] == [nuevo.id]

    async def test_contrato_requisito_upload_through_a_cuenta_still_replaces(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Re-uploading the contract through the CONTRATO checklist requisito is the
        legitimate replace path and must keep working."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            nuevo = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato-v2.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.CONTRATO,
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="CONTRATO",
            )

        assert _CONTRATO_STORAGE_KEY in _deleted_keys(storage)
        assert await db.get(DocumentoFuente, doc_contrato.id) is None
        restantes = await _documentos_contrato(db, contrato.id)
        assert [d.id for d in restantes] == [nuevo.id]

    async def test_replacement_with_no_previous_contract_document_is_a_noop(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            nuevo = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato-v1.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.CONTRATO,
                contrato_id=contrato.id,
                # B3: upload_document requires an EXPLICIT requisito_codigo=="CONTRATO"
                # to treat this call as the contract (this class's whole point is
                # exercising the legitimate replace path via a direct service call,
                # bypassing the router's Opción A/B default synthesis).
                requisito_codigo="CONTRATO",
            )

        storage.delete.assert_not_called()
        restantes = await _documentos_contrato(db, contrato.id)
        assert [d.id for d in restantes] == [nuevo.id]

    async def test_storage_delete_failure_does_not_block_db_replacement(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Storage is best-effort: a delete failure there must NOT abort the replace."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        doc_contrato = await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            storage.delete = AsyncMock(side_effect=RuntimeError("bucket unreachable"))
            mock_storage_cls.return_value = storage

            nuevo = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato-v2.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.CONTRATO,
                contrato_id=contrato.id,
                # B3: upload_document requires an EXPLICIT requisito_codigo=="CONTRATO"
                # to treat this call as the contract (this class's whole point is
                # exercising the legitimate replace path via a direct service call,
                # bypassing the router's Opción A/B default synthesis).
                requisito_codigo="CONTRATO",
            )

        assert await db.get(DocumentoFuente, doc_contrato.id) is None
        restantes = await _documentos_contrato(db, contrato.id)
        assert [d.id for d in restantes] == [nuevo.id]

    async def test_db_delete_failure_is_not_swallowed(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        """The suppress() only covers storage. A DB delete failure must surface, not
        silently leave two 'single source of truth' contract documents behind."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        await _crear_documento_contrato(db, contrato, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            patch.object(db, "delete", new=AsyncMock(side_effect=RuntimeError("db down"))),
        ):
            mock_storage_cls.return_value = _mock_storage()

            with pytest.raises(RuntimeError, match="db down"):
                await upload_document(
                    db=db,
                    user_id=user.id,
                    filename="contrato-v2.txt",
                    content=_TEXTO_SUFICIENTE.encode(),
                    content_type="text/plain",
                    tipo=TipoDocumentoFuente.CONTRATO,
                    contrato_id=contrato.id,
                    requisito_codigo="CONTRATO",
                )


class TestScopedTipoDefault:
    """`tipo` defaulting to `contrato` is what turns an omission into a contract-level
    write. The scope guard above stops the destruction, but an omitted `tipo` still
    stored a cuenta attachment AS the contract text: ``verificar_configuracion_contrato``
    loads every document by ``contrato_id`` regardless of cuenta scope, so it would be
    served to the agent as the contract. The default is therefore resolved per scope:
    cuenta-scoped requests default to the neutral tipo, everything else is unchanged.
    """

    async def test_omitted_tipo_on_a_cuenta_scoped_upload_defaults_to_otros(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={"cuenta_cobro_id": str(cuenta.id), "requisito_codigo": "EVIDENCIAS"},
                files={"file": ("adjunto-generico.pdf", b"%PDF-1.4\nadjunto", "application/pdf")},
            )

        assert r.status_code == 201, r.text
        assert r.json()["tipo"] == "otros"

    async def test_omitted_tipo_on_a_cuenta_scoped_upload_is_not_read_as_contract_text(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.document_service import verificar_configuracion_contrato

        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={"cuenta_cobro_id": str(cuenta.id), "requisito_codigo": "EVIDENCIAS"},
                files={"file": ("adjunto-generico.txt", _TEXTO_SUFICIENTE.encode(), "text/plain")},
            )
        assert r.status_code == 201, r.text

        config = await verificar_configuracion_contrato(db, user.id, contrato.id)
        assert config.tiene_texto_contrato is False

    async def test_omitted_tipo_without_a_cuenta_still_means_contrato(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Backward compatibility: the contract-upload flow keeps its default."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={"contrato_id": str(contrato.id)},
                files={"file": ("contrato-firmado.txt", _TEXTO_SUFICIENTE.encode(), "text/plain")},
            )

        assert r.status_code == 201, r.text
        assert r.json()["tipo"] == "contrato"

    async def test_explicit_tipo_is_always_honoured_on_a_cuenta_scoped_upload(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={
                    "tipo": "seguridad_social",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "SEGURIDAD_SOCIAL",
                },
                files={"file": ("planilla.pdf", b"%PDF-1.4\nplanilla", "application/pdf")},
            )

        assert r.status_code == 201, r.text
        assert r.json()["tipo"] == "seguridad_social"

    async def test_batch_upload_applies_the_same_scoped_default(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=test_user["headers"],
                params={"cuenta_cobro_id": str(cuenta.id), "requisito_codigo": "EVIDENCIAS"},
                files=[("files", ("adjunto-1.pdf", b"%PDF-1.4\nuno", "application/pdf"))],
            )

        assert r.status_code == 201, r.text
        assert [d["tipo"] for d in r.json()] == ["otros"]
