"""Unit tests for contrato_service functions (mocked DB)."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ValidationError


def _make_contrato() -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.usuario_id = uuid.uuid4()
    c.numero_contrato = "CON-001"
    c.entidad = "Entidad Test"
    c.dependencia = "Dep"
    c.supervisor_nombre = "Sup"
    c.objeto = "Objeto del contrato"
    c.fecha_inicio = date(2024, 1, 1)
    c.fecha_fin = date(2024, 6, 30)
    c.valor_total = 12000000
    c.valor_mensual = 2000000
    c.deleted_at = None
    c.obligaciones = []
    c.documento_proveedor = "123456789"
    return c


class TestListarPeriodosPendientes:
    @pytest.mark.asyncio
    async def test_returns_periods_all_pending(self) -> None:
        from app.services.contrato_service import listar_periodos_pendientes

        contrato = _make_contrato()
        contrato.fecha_inicio = date(2024, 1, 1)
        contrato.fecha_fin = date(2024, 3, 31)

        # cuentas_result returns empty (nothing billed)
        cuentas_result = MagicMock()
        cuentas_result.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.return_value = cuentas_result

        with patch(
            "app.services.contrato_service._get_contrato_con_ownership",
            new_callable=AsyncMock,
            return_value=contrato,
        ):
            result = await listar_periodos_pendientes(mock_db, uuid.uuid4(), contrato.id)

        # Should return Jan, Feb, Mar 2024
        assert len(result) == 3
        assert all(p.pendiente for p in result)
        assert result[0].mes == 1
        assert result[2].mes == 3

    @pytest.mark.asyncio
    async def test_marks_billed_period_as_not_pending(self) -> None:
        from app.services.contrato_service import listar_periodos_pendientes

        contrato = _make_contrato()
        contrato.fecha_inicio = date(2024, 1, 1)
        contrato.fecha_fin = date(2024, 2, 28)

        # Simulate Jan 2024 already billed
        billed_row = MagicMock()
        billed_row.anio = 2024
        billed_row.mes = 1

        cuentas_result = MagicMock()
        cuentas_result.all.return_value = [billed_row]

        mock_db = AsyncMock()
        mock_db.execute.return_value = cuentas_result

        with patch(
            "app.services.contrato_service._get_contrato_con_ownership",
            new_callable=AsyncMock,
            return_value=contrato,
        ):
            result = await listar_periodos_pendientes(mock_db, uuid.uuid4(), contrato.id)

        assert len(result) == 2
        jan = next(p for p in result if p.mes == 1)
        feb = next(p for p in result if p.mes == 2)
        assert jan.pendiente is False
        assert feb.pendiente is True


class TestObtenerContextoAgente:
    @pytest.mark.asyncio
    async def test_returns_context_with_no_docs_or_obligations(self) -> None:
        from app.services.contrato_service import obtener_contexto_agente

        contrato = _make_contrato()
        contrato.obligaciones = []

        usuario = MagicMock()
        usuario.id = contrato.usuario_id
        usuario.nombre = "Test User"
        usuario.cedula = "12345678"

        user_result = MagicMock()
        user_result.scalar_one.return_value = usuario

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = []

        cuentas_result = MagicMock()
        cuentas_result.scalars.return_value.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [user_result, docs_result, cuentas_result]

        with patch(
            "app.services.contrato_service._get_contrato_con_ownership",
            new_callable=AsyncMock,
            return_value=contrato,
        ):
            result = await obtener_contexto_agente(mock_db, contrato.usuario_id, contrato.id)

        assert result.listo is False
        assert len(result.faltantes) > 0
        assert result.system_prompt is None
