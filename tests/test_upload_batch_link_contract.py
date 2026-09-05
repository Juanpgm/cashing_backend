"""HTTP contract of `POST /documentos/upload-batch`.

`upload_document` was hardened to raise `ChecklistLinkError` when a document is
persisted but its checklist link fails (A3), but the batch endpoint swallowed it
again: the exception landed in a blanket `except Exception` that turned it into a
`[Error] ...` string appended to the LAST result's `avisos`, and the response was
still 201. The frontend showed green while the requisito stayed Pendiente — the
exact failure A3 set out to remove.

The contract these tests pin down:

* 2xx  → EVERY file was persisted AND linked; `results` has one item per input
         file, in the same order as the input.
* 422  → pre-validation rejected the batch; nothing was persisted.
* 502  → at least one file was persisted but could not be linked
         (`code: "CHECKLIST_LINK_FAILED"`). The remaining files are still
         processed and persisted. Re-uploading the same bytes re-links.
* Every result carries `nombre_original` (the client's filename) so the client can
  match results to inputs without mirroring `get_safe_filename`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, DocumentoRequisitoVinculo
from app.models.documento_fuente import DocumentoFuente
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"
_PDF_MAGIC = b"%PDF-1.4\n"
_ENDPOINT = "/api/v1/documentos/upload-batch"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


def _archivo(nombre: str, semilla: int) -> tuple[str, bytes, str]:
    return (nombre, _PDF_MAGIC + bytes([semilla]) * 2048, "application/pdf")


async def _crear_cuenta(db: AsyncSession, user_id: uuid.UUID) -> CuentaCobro:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-BATCHLINK-001",
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

    from app.services import checklist_service

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    return cuenta


def _params(cuenta: CuentaCobro) -> dict[str, str]:
    return {
        "tipo": "otros",
        "cuenta_cobro_id": str(cuenta.id),
        "requisito_codigo": "EVIDENCIAS",
    }


def _falla_el_enlace_en(indices: set[int]):  # type: ignore[no-untyped-def]
    """Patch context that makes the Nth (1-based) checklist link attempt fail.

    Files are processed in input order, so the Nth link attempt is the Nth file.
    A non-DomainError from `vincular_documento_fuente` is what `upload_document`
    wraps into `ChecklistLinkError`.
    """
    from app.services import checklist_service

    real = checklist_service.vincular_documento_fuente
    contador = {"n": 0}

    async def _flaky(**kwargs: Any) -> Any:
        contador["n"] += 1
        if contador["n"] in indices:
            raise RuntimeError("checklist backend unreachable")
        return await real(**kwargs)

    return patch.object(checklist_service, "vincular_documento_fuente", _flaky)


async def _documentos(db: AsyncSession) -> list[DocumentoFuente]:
    res = await db.execute(select(DocumentoFuente))
    return list(res.scalars().all())


async def _fuentes_vinculadas(db: AsyncSession, cuenta_id: uuid.UUID) -> set[uuid.UUID]:
    res = await db.execute(
        select(DocumentoRequisitoVinculo.documento_fuente_id)
        .join(
            DocumentoCuentaCobro,
            DocumentoCuentaCobro.id == DocumentoRequisitoVinculo.documento_cuenta_cobro_id,
        )
        .where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id)
    )
    return {row[0] for row in res.all() if row[0] is not None}


class TestSingleFileLinkFailure:
    async def test_single_file_that_fails_to_link_returns_502_not_422(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """A one-file batch whose link fails is NOT "all files failed to upload":
        the document IS persisted, only the link is missing."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            _falla_el_enlace_en({1}),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                _ENDPOINT,
                headers=test_user["headers"],
                params=_params(cuenta),
                files=[("files", _archivo("soporte-1.pdf", 1))],
            )

        assert r.status_code == 502, r.text
        cuerpo = r.json()
        assert "soporte-1.pdf" in cuerpo["detail"], cuerpo
        assert cuerpo["code"] == "CHECKLIST_LINK_FAILED", cuerpo

        assert len(await _documentos(db)) == 1, "the document must still be persisted"


class TestPartialLinkFailureFinishesTheBatch:
    async def test_second_of_three_fails_to_link_but_all_three_are_persisted(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            _falla_el_enlace_en({2}),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                _ENDPOINT,
                headers=test_user["headers"],
                params=_params(cuenta),
                files=[
                    ("files", _archivo("soporte-1.pdf", 1)),
                    ("files", _archivo("soporte-2.pdf", 2)),
                    ("files", _archivo("soporte-3.pdf", 3)),
                ],
            )

        assert r.status_code == 502, r.text
        detalle = r.json()["detail"]
        assert "soporte-2.pdf" in detalle, detalle
        assert "soporte-1.pdf" not in detalle and "soporte-3.pdf" not in detalle, detalle

        docs = await _documentos(db)
        assert len(docs) == 3, f"the loop must finish and persist every file: {[d.nombre for d in docs]}"

        por_nombre = {d.nombre: d.id for d in docs}
        vinculadas = await _fuentes_vinculadas(db, cuenta.id)
        assert por_nombre["soporte-1.pdf"] in vinculadas
        assert por_nombre["soporte-3.pdf"] in vinculadas
        assert por_nombre["soporte-2.pdf"] not in vinculadas


class TestRetryRelinks:
    async def test_reuploading_the_same_bytes_relinks_and_returns_2xx(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)
        archivo = _archivo("soporte-1.pdf", 1)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            _falla_el_enlace_en({1}),
        ):
            mock_storage_cls.return_value = _mock_storage()
            primera = await client.post(
                _ENDPOINT, headers=test_user["headers"], params=_params(cuenta), files=[("files", archivo)]
            )
        assert primera.status_code == 502, primera.text

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            reintento = await client.post(
                _ENDPOINT, headers=test_user["headers"], params=_params(cuenta), files=[("files", archivo)]
            )

        assert reintento.status_code == 201, reintento.text
        docs = await _documentos(db)
        assert len(docs) == 1, "the retry must reuse the deduped document, not create a second one"
        assert docs[0].id in await _fuentes_vinculadas(db, cuenta.id), "the retry must repair the link"


def _falla_el_storage_en(indices: set[int]) -> AsyncMock:
    """Storage mock whose upload() raises on the Nth (1-based) call, one per file
    processed in input order — simulates a non-link failure (the document is
    never persisted)."""
    storage = AsyncMock()
    storage.delete = AsyncMock()
    contador = {"n": 0}

    async def _upload(**kwargs: Any) -> None:
        contador["n"] += 1
        if contador["n"] in indices:
            raise RuntimeError("storage backend unreachable")

    storage.upload = AsyncMock(side_effect=_upload)
    return storage


class TestNonLinkFailureIsUnaffectedByLinkHandling:
    async def test_a_storage_failure_alone_still_returns_422_naming_the_file(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """No checklist link failure in this batch: the pre-existing 422 contract
        for a partially-failed batch must be unchanged."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3, return_value=_falla_el_storage_en({2})),
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            r = await client.post(
                _ENDPOINT,
                headers=test_user["headers"],
                params=_params(cuenta),
                files=[
                    ("files", _archivo("soporte-1.pdf", 1)),
                    ("files", _archivo("soporte-2.pdf", 2)),
                ],
            )

        assert r.status_code == 422, r.text
        cuerpo = r.json()
        assert "soporte-2.pdf" in cuerpo["detail"], cuerpo

        docs = await _documentos(db)
        assert len(docs) == 1, f"only the file that did not fail must be persisted: {[d.nombre for d in docs]}"


class TestMixedLinkAndNonLinkFailures:
    async def test_502_detail_names_the_unlinked_file_and_the_unsaved_file_separately(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Three files: #1 persists and links fine, #2 persists but fails to link,
        #3 never persists at all (storage failure). Before this fix, `link_failed`
        short-circuited the response and #3 was never named anywhere — the
        frontend then marked a file that was never saved as done."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3, return_value=_falla_el_storage_en({3})),
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            _falla_el_enlace_en({2}),
        ):
            r = await client.post(
                _ENDPOINT,
                headers=test_user["headers"],
                params=_params(cuenta),
                files=[
                    ("files", _archivo("soporte-1.pdf", 1)),
                    ("files", _archivo("soporte-2.pdf", 2)),
                    ("files", _archivo("soporte-3.pdf", 3)),
                ],
            )

        assert r.status_code == 502, r.text
        cuerpo = r.json()
        assert cuerpo["code"] == "CHECKLIST_LINK_FAILED", cuerpo
        detalle = cuerpo["detail"]
        assert "'soporte-2.pdf'" in detalle, detalle
        assert "'soporte-3.pdf'" in detalle, detalle
        assert "soporte-1.pdf" not in detalle, detalle

        docs = await _documentos(db)
        por_nombre = {d.nombre for d in docs}
        assert por_nombre == {"soporte-1.pdf", "soporte-2.pdf"}, (
            f"soporte-3.pdf must never have been persisted: {por_nombre}"
        )


class TestResultsCarryTheOriginalFilename:
    async def test_results_are_in_input_order_and_expose_nombre_original(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """The stored `nombre` is sanitized (`get_safe_filename`), so the client
        cannot match results to inputs by name without reimplementing it."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)
        nombres = ["1. Informe de actividades.pdf", "acta inicio.pdf", "RPC (adición).pdf"]

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                _ENDPOINT,
                headers=test_user["headers"],
                params=_params(cuenta),
                files=[("files", _archivo(nombre, i)) for i, nombre in enumerate(nombres, start=1)],
            )

        assert r.status_code == 201, r.text
        cuerpo = r.json()
        assert len(cuerpo) == len(nombres), cuerpo
        assert [item["nombre_original"] for item in cuerpo] == nombres, cuerpo
        assert cuerpo[0]["nombre"] != nombres[0], "precondition: the stored name is sanitized"
