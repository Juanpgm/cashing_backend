"""Tests for secop_service public functions with mocked _query_socrata."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ValidationError


def _make_db_with_empty_result():
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []

    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    return mock_db


def _make_fake_contrato():
    from app.models.secop import SecopContrato
    c = MagicMock(spec=SecopContrato)
    c.id = uuid.uuid4()
    c.id_contrato_secop = "CO1.PCCNTR.001"
    c.cedula_contratista = "12345678"
    c.nombre_contratista = "Juan Perez"
    c.nombre_entidad = "MinTIC"
    c.tipo_de_contrato = "Prestación de Servicios"
    c.numero_contrato = "CO1.PCCNTR.001"
    c.proceso_de_compra = None
    c.updated_at = datetime(2020, 1, 1, tzinfo=UTC)  # stale
    return c


class TestBuscarContratoCedulaCached:
    @pytest.mark.asyncio
    async def test_returns_empty_when_fresh_cache_exists_no_servicios(self) -> None:
        from app.services.secop_service import buscar_contratos_cedula

        # Build a mock contrato with non-prestacion type
        c = _make_fake_contrato()
        c.tipo_de_contrato = "Compraventa"
        c.updated_at = datetime.now(tz=UTC)  # fresh

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [c]

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        result = await buscar_contratos_cedula(mock_db, "12345678")
        assert result == []

    @pytest.mark.asyncio
    async def test_refreshes_when_stale_cache(self) -> None:
        from app.services.secop_service import buscar_contratos_cedula

        # First execute → stale cached contrato
        c = _make_fake_contrato()
        c.tipo_de_contrato = "Prestación de Servicios"
        c.updated_at = datetime(2020, 1, 1, tzinfo=UTC)

        first_result = MagicMock()
        first_result.scalars.return_value.all.return_value = [c]

        # Second execute after refresh → empty list (avoids model_validate on MagicMock)
        refreshed_result = MagicMock()
        refreshed_result.scalars.return_value.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [first_result, refreshed_result]

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await buscar_contratos_cedula(mock_db, "12345678")

        # Should have called commit after refresh
        mock_db.commit.assert_called_once()
        assert result == []


class TestImportarContratosValidation:
    @pytest.mark.asyncio
    async def test_raises_validation_error_for_invalid_cedula(self) -> None:
        from app.services.secop_service import importar_contratos_secop

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await importar_contratos_secop(mock_db, "abc", uuid.uuid4())

    @pytest.mark.asyncio
    async def test_returns_result_with_no_rows(self) -> None:
        from app.services.secop_service import importar_contratos_secop

        mock_existing = MagicMock()
        mock_existing.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_existing

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await importar_contratos_secop(mock_db, "12345678", uuid.uuid4())

        assert result.encontrados_en_secop == 0
        assert result.importados == 0


class TestSincronizarDocumentosValidation:
    @pytest.mark.asyncio
    async def test_raises_validation_error_for_invalid_cedula(self) -> None:
        from app.services.secop_service import sincronizar_documentos_secop

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await sincronizar_documentos_secop(mock_db, "short")

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_contratos(self) -> None:
        from app.services.secop_service import sincronizar_documentos_secop

        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []
        empty_result.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.return_value = empty_result

        result = await sincronizar_documentos_secop(mock_db, "12345678")
        assert result.documentos_guardados == 0


class TestObtenerProcesoMocked:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_in_cache_and_socrata_empty(self) -> None:
        from app.services.secop_service import obtener_proceso

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await obtener_proceso(mock_db, "CO1.BDOS.001")

        assert result is None


class TestConsultaCompleta:
    @pytest.mark.asyncio
    async def test_raises_validation_error_for_invalid_cedula(self) -> None:
        from app.services.secop_service import consulta_completa

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await consulta_completa(mock_db, "no")
