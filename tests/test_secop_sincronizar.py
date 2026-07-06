"""Tests for secop_service.sincronizar_documentos_secop."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSincronizarDocumentosSecop:
    @pytest.mark.asyncio
    async def test_raises_validation_error_for_invalid_cedula(self) -> None:
        from app.core.exceptions import ValidationError
        from app.services.secop_service import sincronizar_documentos_secop

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await sincronizar_documentos_secop(mock_db, "abc-invalid")

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cached_contratos(self) -> None:
        from app.services.secop_service import sincronizar_documentos_secop

        contratos_result = MagicMock()
        contratos_result.scalars.return_value.all.return_value = []

        existing_ids_result = MagicMock()
        existing_ids_result.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contratos_result, existing_ids_result]

        result = await sincronizar_documentos_secop(mock_db, "12345678", confirmar=False)

        assert result.documentos_guardados == 0
        assert result.documentos == []

    @pytest.mark.asyncio
    async def test_preview_fetches_docs_without_persisting(self) -> None:
        from app.services.secop_service import sincronizar_documentos_secop

        contrato = MagicMock()
        contrato.id = uuid.uuid4()
        contrato.numero_contrato = "CON-001"
        contrato.proceso_de_compra = None  # no proceso

        contratos_result = MagicMock()
        contratos_result.scalars.return_value.all.return_value = [contrato]

        existing_ids_result = MagicMock()
        existing_ids_result.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contratos_result, existing_ids_result]

        secop_doc_row = {
            "id_documento": "DOC-001",
            "n_mero_de_contrato": "CON-001",
            "proceso": None,
            "nombre_archivo": "contrato.pdf",
            "extensi_n": "pdf",
            "descripci_n": "Minuta del contrato",
            "url_descarga_documento": "https://example.com/doc.pdf",
            "fecha_carga": "2024-01-15",
            "entidad": "Alcaldia",
            "nit_entidad": "899999061",
            "tamanno_archivo": "1024",
        }

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_doc_row],
        ):
            result = await sincronizar_documentos_secop(mock_db, "12345678", confirmar=False)

        assert result.documentos_guardados == 1
        assert len(result.documentos) == 1
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_loads_procesos_when_contratos_have_proceso_ids(self) -> None:
        """When contrato has proceso_de_compra, secop_procesos are loaded from DB."""
        from app.services.secop_service import sincronizar_documentos_secop

        contrato = MagicMock()
        contrato.id = uuid.uuid4()
        contrato.numero_contrato = "CON-003"
        contrato.proceso_de_compra = "PROC-123"

        contratos_result = MagicMock()
        contratos_result.scalars.return_value.all.return_value = [contrato]

        proceso = MagicMock()
        proceso.id = uuid.uuid4()
        proceso.id_proceso_secop = "PROC-123"

        procesos_result = MagicMock()
        procesos_result.scalars.return_value.all.return_value = [proceso]

        existing_ids_result = MagicMock()
        existing_ids_result.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contratos_result, procesos_result, existing_ids_result]

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await sincronizar_documentos_secop(mock_db, "12345678", confirmar=False)

        assert result.documentos_guardados == 0
        assert result.contratos_procesados == 1

    @pytest.mark.asyncio
    async def test_skips_duplicate_existing_docs_in_confirm_mode(self) -> None:
        """Docs with IDs already in DB are counted as omitidos in confirm mode."""
        from app.services.secop_service import sincronizar_documentos_secop

        contrato = MagicMock()
        contrato.id = uuid.uuid4()
        contrato.numero_contrato = "CON-004"
        contrato.proceso_de_compra = None

        contratos_result = MagicMock()
        contratos_result.scalars.return_value.all.return_value = [contrato]

        # DOC-EXISTING is already in DB
        existing_ids_result = MagicMock()
        existing_ids_result.all.return_value = [("DOC-EXISTING",)]

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contratos_result, existing_ids_result]

        secop_doc_row = {
            "id_documento": "DOC-EXISTING",
            "n_mero_de_contrato": "CON-004",
            "proceso": None,
            "nombre_archivo": "existing.pdf",
            "extensi_n": "pdf",
            "descripci_n": "Already uploaded",
            "url_descarga_documento": None,
        }

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_doc_row],
        ):
            # confirmar=True skips the SecopDocumento() preview path for existing IDs
            result = await sincronizar_documentos_secop(mock_db, "12345678", confirmar=True)

        # In confirm mode, existing docs are still omitted (not upserted again)
        assert result.documentos_omitidos_duplicados == 1
        from app.services.secop_service import sincronizar_documentos_secop

        contrato = MagicMock()
        contrato.id = uuid.uuid4()
        contrato.numero_contrato = "CON-002"
        contrato.proceso_de_compra = None

        contratos_result = MagicMock()
        contratos_result.scalars.return_value.all.return_value = [contrato]

        existing_ids_result = MagicMock()
        existing_ids_result.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contratos_result, existing_ids_result]

        secop_doc_row = {
            "id_documento": "DOC-002",
            "n_mero_de_contrato": "CON-002",
            "proceso": None,
            "nombre_archivo": "addenda.pdf",
            "extensi_n": "pdf",
            "descripci_n": "Adenda",
            "url_descarga_documento": "https://example.com/addenda.pdf",
        }

        fake_upserted_doc = MagicMock()
        fake_upserted_doc.id = uuid.uuid4()
        fake_upserted_doc.id_documento_secop = "DOC-002"
        fake_upserted_doc.numero_contrato = "CON-002"
        fake_upserted_doc.proceso = None
        fake_upserted_doc.secop_contrato_id = contrato.id
        fake_upserted_doc.secop_proceso_id = None
        fake_upserted_doc.nombre_archivo = "addenda.pdf"
        fake_upserted_doc.extension = "pdf"
        fake_upserted_doc.descripcion = "Adenda"
        fake_upserted_doc.url_descarga = "https://example.com/addenda.pdf"
        fake_upserted_doc.fecha_carga = None
        fake_upserted_doc.entidad = None
        fake_upserted_doc.nit_entidad = None

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_doc_row],
        ):
            with patch(
                "app.services.secop_service._upsert_documento",
                new_callable=AsyncMock,
                return_value=fake_upserted_doc,
            ):
                result = await sincronizar_documentos_secop(mock_db, "12345678", confirmar=True)

        mock_db.commit.assert_called()
        assert result.documentos_guardados == 1
