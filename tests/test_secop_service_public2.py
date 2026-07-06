"""Tests for secop_service helper public functions: buscar_contratos_cedula, obtener_proceso, consulta_completa."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_secop_contrato_orm(numero: str = "CON-001") -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.numero_contrato = numero
    c.proceso_de_compra = None
    c.tipo_de_contrato = "Prestación de Servicios"
    c.updated_at = None
    return c


class TestBuscarContratosCedula:
    @pytest.mark.asyncio
    async def test_returns_cached_when_fresh(self) -> None:
        from app.services.secop_service import buscar_contratos_cedula

        cached_contrato = _make_secop_contrato_orm()

        result_1 = MagicMock()
        result_1.scalars.return_value.all.return_value = [cached_contrato]

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_1

        with patch("app.services.secop_service._is_fresh", return_value=True):
            with patch(
                "app.services.secop_service.SecopContratoResponse.model_validate",
                return_value=MagicMock(),
            ):
                with patch(
                    "app.services.secop_service._is_prestacion_servicios",
                    return_value=True,
                ):
                    result = await buscar_contratos_cedula(mock_db, "12345678", refresh=False)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_raises_validation_error_for_invalid_cedula(self) -> None:
        from app.core.exceptions import ValidationError
        from app.services.secop_service import buscar_contratos_cedula

        mock_db = AsyncMock()
        with pytest.raises(ValidationError):
            await buscar_contratos_cedula(mock_db, "abc-invalid")

    @pytest.mark.asyncio
    async def test_queries_socrata_when_cache_empty(self) -> None:
        from app.services.secop_service import buscar_contratos_cedula

        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        cached_contrato = _make_secop_contrato_orm()
        filled_result = MagicMock()
        filled_result.scalars.return_value.all.return_value = [cached_contrato]

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [empty_result, filled_result]

        secop_row = {"documento_proveedor": "12345678", "numero_contrato": "CON-001"}

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_row],
        ):
            with patch("app.services.secop_service._upsert_contrato", new_callable=AsyncMock):
                with patch(
                    "app.services.secop_service.SecopContratoResponse.model_validate",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "app.services.secop_service._is_prestacion_servicios",
                        return_value=True,
                    ):
                        result = await buscar_contratos_cedula(mock_db, "12345678", refresh=True)

        mock_db.commit.assert_called()
        assert len(result) == 1


class TestObtenerProceso:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_in_secop(self) -> None:
        from app.services.secop_service import obtener_proceso

        cached_result = MagicMock()
        cached_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = cached_result

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await obtener_proceso(mock_db, "PROC-001")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_cached_proceso_when_fresh(self) -> None:
        from app.services.secop_service import obtener_proceso

        proceso = MagicMock()
        proceso.updated_at = None

        cached_result = MagicMock()
        cached_result.scalar_one_or_none.return_value = proceso

        mock_db = AsyncMock()
        mock_db.execute.return_value = cached_result

        with patch("app.services.secop_service._is_fresh", return_value=True):
            with patch(
                "app.services.secop_service.SecopProcesoResponse.model_validate",
                return_value=MagicMock(),
            ):
                result = await obtener_proceso(mock_db, "PROC-001")

        assert result is not None

    @pytest.mark.asyncio
    async def test_fetches_and_upserts_when_stale(self) -> None:
        from app.services.secop_service import obtener_proceso

        cached_result = MagicMock()
        cached_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = cached_result

        fake_proceso = MagicMock()
        secop_row = {"id_del_portafolio": "PROC-001"}

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_row],
        ):
            with patch(
                "app.services.secop_service._upsert_proceso",
                new_callable=AsyncMock,
                return_value=fake_proceso,
            ):
                with patch(
                    "app.services.secop_service.SecopProcesoResponse.model_validate",
                    return_value=MagicMock(),
                ):
                    result = await obtener_proceso(mock_db, "PROC-001")

        mock_db.commit.assert_called()
        assert result is not None


class TestConsultaCompleta:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_contracts(self) -> None:
        from app.services.secop_service import consulta_completa

        with patch(
            "app.services.secop_service.buscar_contratos_cedula",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await consulta_completa(MagicMock(), "12345678")

        assert result.total_contratos == 0
        assert result.contratos == []

    @pytest.mark.asyncio
    async def test_enriches_contracts_with_process_and_docs(self) -> None:
        from app.services.secop_service import consulta_completa

        fake_contrato = MagicMock()
        fake_contrato.proceso_de_compra = "PROC-001"
        fake_contrato.numero_contrato = "CON-001"

        fake_detalle = MagicMock()

        with patch(
            "app.services.secop_service.buscar_contratos_cedula",
            new_callable=AsyncMock,
            return_value=[fake_contrato],
        ):
            with patch(
                "app.services.secop_service.obtener_proceso",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ):
                with patch(
                    "app.services.secop_service.buscar_documentos_contrato",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    with patch(
                        "app.services.secop_service.SecopContratoDetalleResponse",
                        return_value=fake_detalle,
                    ):
                        with patch(
                            "app.services.secop_service.SecopConsultaCompletaResponse",
                        ) as mock_resp_cls:
                            mock_resp_cls.return_value = MagicMock(
                                total_contratos=1,
                                contratos=[fake_detalle],
                                cedula="12345678",
                            )
                            result = await consulta_completa(MagicMock(), "12345678")

        assert result.total_contratos == 1
        assert len(result.contratos) == 1
