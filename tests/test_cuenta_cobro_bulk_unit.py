"""Tests for cuenta_cobro_service bulk/desde_texto functions (mocked DB)."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ValidationError
from app.models.cuenta_cobro import EstadoCuentaCobro


def _make_cuenta(estado: EstadoCuentaCobro = EstadoCuentaCobro.BORRADOR) -> MagicMock:
    cuenta = MagicMock()
    cuenta.id = uuid.uuid4()
    cuenta.contrato_id = uuid.uuid4()
    cuenta.contrato = MagicMock()
    cuenta.estado = estado
    cuenta.mes = 3
    cuenta.anio = 2024
    return cuenta


def _make_actividad(cuenta_id: uuid.UUID) -> MagicMock:
    act = MagicMock()
    act.id = uuid.uuid4()
    act.cuenta_cobro_id = cuenta_id
    act.obligacion_id = None
    act.descripcion = "Actividad de prueba para el mes"
    act.justificacion = "Se realizó la actividad"
    act.fecha_realizacion = date(2024, 3, 15)
    act.created_at = None
    act.updated_at = None
    return act


class TestAgregarActividadesBulk:
    @pytest.mark.asyncio
    async def test_raises_on_wrong_state(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_bulk
        from app.schemas.cuenta_cobro import ActividadCreate

        cuenta = _make_cuenta(EstadoCuentaCobro.APROBADA)
        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            with pytest.raises(ValidationError, match="borrador"):
                await agregar_actividades_bulk(
                    AsyncMock(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    [ActividadCreate(descripcion="Hice algo importante", fecha_realizacion=date(2024, 3, 1))],
                )

    @pytest.mark.asyncio
    async def test_creates_activities_no_obligaciones(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_bulk
        from app.schemas.cuenta_cobro import ActividadCreate

        cuenta = _make_cuenta()
        actividad_data = ActividadCreate(
            descripcion="Elabore el informe mensual del proyecto",
            justificacion="Se entrego a tiempo",
            fecha_realizacion=date(2024, 3, 15),
        )

        mock_db = AsyncMock()
        fake_act_response = MagicMock()
        fake_bulk_response = MagicMock()
        fake_bulk_response.creadas = 1

        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            with patch("app.services.cuenta_cobro_service.ActividadResponse") as mock_act_resp:
                mock_act_resp.model_validate.return_value = fake_act_response
                with patch(
                    "app.services.cuenta_cobro_service.ActividadesBulkResponse",
                    return_value=fake_bulk_response,
                ):
                    result = await agregar_actividades_bulk(
                        mock_db,
                        uuid.uuid4(),
                        cuenta.id,
                        [actividad_data],
                    )

        assert result.creadas == 1

    @pytest.mark.asyncio
    async def test_raises_when_obligacion_not_found(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_bulk
        from app.schemas.cuenta_cobro import ActividadCreate

        cuenta = _make_cuenta()
        ob_id = uuid.uuid4()
        actividad_data = ActividadCreate(
            descripcion="Tarea con obligación inválida que no existe",
            obligacion_id=ob_id,
            fecha_realizacion=date(2024, 3, 1),
        )

        mock_db = AsyncMock()
        # DB returns empty scalars (obligacion not found)
        ob_result = MagicMock()
        ob_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = ob_result

        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            from app.core.exceptions import NotFoundError
            with pytest.raises(NotFoundError):
                await agregar_actividades_bulk(
                    mock_db,
                    uuid.uuid4(),
                    cuenta.id,
                    [actividad_data],
                )


class TestAgregarActividadesDesdeTexto:
    @pytest.mark.asyncio
    async def test_raises_on_wrong_state(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_desde_texto

        cuenta = _make_cuenta(EstadoCuentaCobro.ENVIADA)
        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            with pytest.raises(ValidationError, match="borrador"):
                await agregar_actividades_desde_texto(
                    AsyncMock(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    "1. Actividad",
                    None,
                    False,
                )

    @pytest.mark.asyncio
    async def test_raises_when_no_numbered_lines(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_desde_texto

        cuenta = _make_cuenta()
        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            with pytest.raises(ValidationError, match="número"):
                await agregar_actividades_desde_texto(
                    AsyncMock(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    "Esto no tiene números",
                    None,
                    False,
                )

    @pytest.mark.asyncio
    async def test_raises_when_line_too_short(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_desde_texto

        cuenta = _make_cuenta()
        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            with pytest.raises(ValidationError, match="corta"):
                await agregar_actividades_desde_texto(
                    AsyncMock(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    "1. Corto",
                    None,
                    False,
                )

    @pytest.mark.asyncio
    async def test_creates_activities_from_text(self) -> None:
        from app.services.cuenta_cobro_service import agregar_actividades_desde_texto

        cuenta = _make_cuenta()
        mock_db = AsyncMock()
        fake_act_response = MagicMock()
        fake_bulk_response = MagicMock()
        fake_bulk_response.creadas = 1

        with patch(
            "app.services.cuenta_cobro_service._get_cuenta_con_ownership",
            new_callable=AsyncMock,
            return_value=cuenta,
        ):
            with patch("app.services.cuenta_cobro_service.ActividadResponse") as mock_act_resp:
                mock_act_resp.model_validate.return_value = fake_act_response
                with patch(
                    "app.services.cuenta_cobro_service.ActividadesBulkResponse",
                    return_value=fake_bulk_response,
                ):
                    result = await agregar_actividades_desde_texto(
                        mock_db,
                        uuid.uuid4(),
                        cuenta.id,
                        "1. Elabore el informe mensual detallado del proyecto para la entidad",
                        date(2024, 3, 31),
                        False,
                    )

        assert result.creadas == 1
