"""Additional tests for secop_service: validation and upsert helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import ValidationError


class TestBuscarContratoCedulaValidation:
    @pytest.mark.asyncio
    async def test_raises_validation_error_for_non_numeric_cedula(self) -> None:
        from app.services.secop_service import buscar_contratos_cedula

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await buscar_contratos_cedula(mock_db, "abc")

    @pytest.mark.asyncio
    async def test_raises_validation_error_for_too_short_cedula(self) -> None:
        from app.services.secop_service import buscar_contratos_cedula

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await buscar_contratos_cedula(mock_db, "123")


class TestUpsertContrato:
    @pytest.mark.asyncio
    async def test_returns_none_when_id_contrato_missing(self) -> None:
        from app.services.secop_service import _upsert_contrato

        mock_db = AsyncMock()
        result = await _upsert_contrato(mock_db, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_creates_new_contrato_when_not_exists(self) -> None:
        from app.services.secop_service import _upsert_contrato

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        row = {
            "id_contrato": "CO1.PCCNTR.001",
            "documento_proveedor": "12345678",
            "proveedor_adjudicado": "Empresa S.A.",
            "nombre_entidad": "MinTIC",
            "tipo_de_contrato": "Prestación de Servicios",
        }
        obj = await _upsert_contrato(mock_db, row)
        assert obj is not None
        assert obj.id_contrato_secop == "CO1.PCCNTR.001"
        mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_existing_contrato(self) -> None:
        from app.services.secop_service import _upsert_contrato

        existing = MagicMock()
        existing.id_contrato_secop = "CO1.PCCNTR.001"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        row = {
            "id_contrato": "CO1.PCCNTR.001",
            "documento_proveedor": "12345678",
            "nombre_entidad": "MinDefensa",
        }
        obj = await _upsert_contrato(mock_db, row)
        assert obj is existing
        mock_db.add.assert_not_called()


class TestUpsertProceso:
    @pytest.mark.asyncio
    async def test_returns_none_when_id_portafolio_missing(self) -> None:
        from app.services.secop_service import _upsert_proceso

        mock_db = AsyncMock()
        result = await _upsert_proceso(mock_db, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_creates_new_proceso(self) -> None:
        from app.services.secop_service import _upsert_proceso

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        row = {
            "id_del_portafolio": "CO1.BDOS.001",
            "nombre_del_procedimiento": "Contratación TI",
            "entidad": "MinTIC",
        }
        obj = await _upsert_proceso(mock_db, row)
        assert obj is not None
        assert obj.id_proceso_secop == "CO1.BDOS.001"


class TestUpsertDocumento:
    @pytest.mark.asyncio
    async def test_returns_none_when_id_documento_missing(self) -> None:
        from app.services.secop_service import _upsert_documento

        mock_db = AsyncMock()
        result = await _upsert_documento(mock_db, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_wires_extension_and_tipo_origen_into_clasificacion(self) -> None:
        """R4: _upsert_documento already sets obj.extension/obj.datos_raw (with
        _tipo_origen staged by the caller) BEFORE calling aplicar_clasificacion —
        so the R4 metadata prior (modificacion demotes CONTRATO) applies without
        any extra wiring in this file. Regression guard for that wiring staying
        intact as document_classifier.clasificar()'s signature grows."""
        from app.models.categoria_documento import CategoriaDocumento
        from app.services.secop_service import _upsert_documento

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        row = {
            "id_documento": "DOC-PRIORS-1",
            "nombre_archivo": "contrato.pdf",
            "extensi_n": "pdf",
            "_tipo_origen": "modificacion",
        }
        obj = await _upsert_documento(mock_db, row)

        assert obj is not None
        assert obj.categoria == CategoriaDocumento.CONTRATO
        # PRIMARY_BASE (0.750) - ADJ_MODIFICACION_CONTRATO (0.150) = 0.600
        assert obj.categoria_confianza == pytest.approx(0.600)
