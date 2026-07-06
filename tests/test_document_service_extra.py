"""Unit tests for document_service.py functions not covered by existing tests."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _obtener_obligaciones_existentes
# ---------------------------------------------------------------------------


class TestObtenerObligacionesExistentes:
    @pytest.mark.asyncio
    async def test_returns_obligations_from_db(self) -> None:
        from app.services.document_service import _obtener_obligaciones_existentes

        ob = MagicMock()
        ob.descripcion = "Entregar informes mensuales"
        ob.tipo = MagicMock()
        ob.tipo.value = "entregable"
        ob.orden = 1

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [ob]

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        contrato_id = uuid.uuid4()
        result = await _obtener_obligaciones_existentes(contrato_id, mock_db)

        assert len(result) == 1
        assert result[0].descripcion == "Entregar informes mensuales"
        assert result[0].tipo == "entregable"
        assert result[0].orden == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_obligations(self) -> None:
        from app.services.document_service import _obtener_obligaciones_existentes

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        result = await _obtener_obligaciones_existentes(uuid.uuid4(), mock_db)
        assert result == []


# ---------------------------------------------------------------------------
# process_document
# ---------------------------------------------------------------------------


class TestProcessDocument:
    @pytest.mark.asyncio
    async def test_raises_not_found_when_doc_missing(self) -> None:
        from app.core.exceptions import NotFoundError
        from app.services.document_service import process_document

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        with pytest.raises(NotFoundError):
            await process_document(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_reprocesses_existing_document(self) -> None:
        from app.services.document_service import process_document

        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.storage_key = "usuarios/abc/documentos/file.pdf"
        doc.nombre = "contrato.pdf"
        doc.metadata_json = {}
        doc.texto_extraido = "old text"

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = doc

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        with patch(
            "app.services.document_service._get_storage"
        ) as mock_s3_cls:
            mock_s3 = AsyncMock()
            mock_s3.download.return_value = b"%PDF-content"
            mock_s3_cls.return_value = mock_s3

            with patch(
                "app.services.document_service.parse_document",
                return_value="new extracted text",
            ):
                result = await process_document(mock_db, uuid.uuid4(), uuid.uuid4())

        assert result.texto_extraido == "new extracted text"
        assert doc.texto_extraido == "new extracted text"
        mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# listar_documentos_contrato
# ---------------------------------------------------------------------------


class TestListarDocumentosContrato:
    @pytest.mark.asyncio
    async def test_raises_not_found_when_contrato_missing(self) -> None:
        from app.core.exceptions import NotFoundError
        from app.services.document_service import listar_documentos_contrato

        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = contrato_result

        with pytest.raises(NotFoundError):
            await listar_documentos_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_returns_documents_for_contract(self) -> None:
        from app.services.document_service import listar_documentos_contrato

        contrato = MagicMock()
        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = contrato

        from app.models.documento_fuente import TipoDocumentoFuente

        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.nombre = "contrato.pdf"
        doc.tipo = TipoDocumentoFuente.CONTRATO
        doc.contrato_id = uuid.uuid4()
        doc.texto_extraido = "some text"
        doc.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = [doc]

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contrato_result, docs_result]

        result = await listar_documentos_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

        assert len(result) == 1
        assert result[0].nombre == "contrato.pdf"
        assert result[0].tiene_texto is True


# ---------------------------------------------------------------------------
# verificar_configuracion_contrato
# ---------------------------------------------------------------------------


class TestVerificarConfiguracionContrato:
    def _make_contrato(self) -> MagicMock:
        c = MagicMock()
        c.id = uuid.uuid4()
        c.numero_contrato = "CON-001"
        c.objeto = "Prestacion de servicios"
        c.entidad = "Alcaldia"
        c.dependencia = "Sec. TIC"
        c.supervisor_nombre = "Supervisor"
        c.fecha_inicio = date(2024, 1, 1)
        c.fecha_fin = date(2024, 12, 31)
        c.valor_total = 12000000
        c.valor_mensual = 1000000
        c.obligaciones = []
        return c

    @pytest.mark.asyncio
    async def test_raises_not_found_when_contrato_missing(self) -> None:
        from app.core.exceptions import NotFoundError
        from app.services.document_service import verificar_configuracion_contrato

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        with pytest.raises(NotFoundError):
            await verificar_configuracion_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_returns_not_ready_when_no_documents(self) -> None:
        from app.services.document_service import verificar_configuracion_contrato

        contrato = self._make_contrato()

        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = contrato

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = []

        plantilla_result = MagicMock()
        plantilla_result.scalars.return_value.first.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contrato_result, docs_result, plantilla_result]

        result = await verificar_configuracion_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

        assert result.listo is False
        assert result.tiene_texto_contrato is False
        assert result.tiene_instrucciones is False
        assert len(result.faltantes) > 0

    @pytest.mark.asyncio
    async def test_returns_ready_when_all_present(self) -> None:
        from app.models.documento_fuente import TipoDocumentoFuente
        from app.services.document_service import verificar_configuracion_contrato

        contrato = self._make_contrato()

        # Add obligation
        ob = MagicMock()
        ob.tipo = MagicMock()
        ob.tipo.value = "entregable"
        ob.descripcion = "Entregar informes"
        ob.orden = 1
        contrato.obligaciones = [ob]

        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = contrato

        doc_contrato = MagicMock()
        doc_contrato.id = uuid.uuid4()
        doc_contrato.nombre = "contrato.pdf"
        doc_contrato.tipo = TipoDocumentoFuente.CONTRATO
        doc_contrato.contrato_id = contrato.id
        doc_contrato.texto_extraido = "Texto del contrato con obligaciones y clausulas"
        doc_contrato.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        doc_instrucciones = MagicMock()
        doc_instrucciones.id = uuid.uuid4()
        doc_instrucciones.nombre = "instrucciones.txt"
        doc_instrucciones.tipo = TipoDocumentoFuente.INSTRUCCIONES
        doc_instrucciones.contrato_id = contrato.id
        doc_instrucciones.texto_extraido = "Instrucciones de trabajo"
        doc_instrucciones.created_at = datetime(2024, 1, 2, tzinfo=UTC)

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = [doc_contrato, doc_instrucciones]

        plantilla_result = MagicMock()
        plantilla_result.scalars.return_value.first.return_value = MagicMock()  # plantilla exists

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contrato_result, docs_result, plantilla_result]

        result = await verificar_configuracion_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

        assert result.listo is True
        assert result.tiene_texto_contrato is True
        assert result.tiene_instrucciones is True
        assert result.tiene_obligaciones is True
        assert result.faltantes == []
