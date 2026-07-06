"""Tests for cuenta_cobro_service pure helper functions."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.cuenta_cobro import EstadoCuentaCobro


class TestParseActividadesLlm:
    def test_parses_valid_actividad_lines(self) -> None:
        from app.services.cuenta_cobro_service import _parse_actividades_llm

        response = (
            "ACTIVIDAD|Elaborar informe mensual|Se entregó informe completo|1\n"
            "ACTIVIDAD|Reunión de seguimiento|Asistencia registrada|2\n"
        )
        ob1 = MagicMock()
        ob1.id = uuid.uuid4()
        ob2 = MagicMock()
        ob2.id = uuid.uuid4()

        result = _parse_actividades_llm(response, [ob1, ob2])
        assert len(result) == 2
        assert result[0].descripcion == "Elaborar informe mensual"
        assert result[0].obligacion_id == ob1.id
        assert result[1].obligacion_id == ob2.id

    def test_skips_short_descriptions(self) -> None:
        from app.services.cuenta_cobro_service import _parse_actividades_llm

        response = "ACTIVIDAD|Short|justificacion larga aqui|1\n"
        result = _parse_actividades_llm(response, [])
        # "Short" is < 10 chars → skipped
        assert result == []

    def test_out_of_bounds_obligation_index_sets_none(self) -> None:
        from app.services.cuenta_cobro_service import _parse_actividades_llm

        response = "ACTIVIDAD|Descripcion suficientemente larga|Justificacion aqui|5\n"
        ob = MagicMock()
        ob.id = uuid.uuid4()

        result = _parse_actividades_llm(response, [ob])
        # index 5-1=4 is out of bounds → obligacion_id=None
        assert len(result) == 1
        assert result[0].obligacion_id is None

    def test_empty_string_returns_empty_list(self) -> None:
        from app.services.cuenta_cobro_service import _parse_actividades_llm

        result = _parse_actividades_llm("", [])
        assert result == []

    def test_with_no_obligations(self) -> None:
        from app.services.cuenta_cobro_service import _parse_actividades_llm

        response = "ACTIVIDAD|Descripcion suficientemente larga|Justificacion|1\n"
        result = _parse_actividades_llm(response, [])
        assert len(result) == 1
        assert result[0].obligacion_id is None


class TestTransiciones:
    def test_borrador_to_enviada_valid(self) -> None:
        from app.services.cuenta_cobro_service import _TRANSICIONES

        assert EstadoCuentaCobro.ENVIADA in _TRANSICIONES[EstadoCuentaCobro.BORRADOR]

    def test_pagada_has_no_transitions(self) -> None:
        from app.services.cuenta_cobro_service import _TRANSICIONES

        assert len(_TRANSICIONES[EstadoCuentaCobro.PAGADA]) == 0

    def test_rechazada_can_go_back_to_borrador(self) -> None:
        from app.services.cuenta_cobro_service import _TRANSICIONES

        assert EstadoCuentaCobro.BORRADOR in _TRANSICIONES[EstadoCuentaCobro.RECHAZADA]


class TestGetCuentaConOwnership:
    @pytest.mark.asyncio
    async def test_raises_not_found_when_missing(self) -> None:
        from unittest.mock import AsyncMock

        from app.services.cuenta_cobro_service import _get_cuenta_con_ownership

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        with pytest.raises(NotFoundError):
            await _get_cuenta_con_ownership(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_raises_forbidden_when_wrong_owner(self) -> None:
        from unittest.mock import AsyncMock

        from app.services.cuenta_cobro_service import _get_cuenta_con_ownership

        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()

        fake_contrato = MagicMock()
        fake_contrato.usuario_id = other_user_id

        fake_cuenta = MagicMock()
        fake_cuenta.contrato = fake_contrato

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_cuenta

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        with pytest.raises(ForbiddenError):
            await _get_cuenta_con_ownership(mock_db, user_id, uuid.uuid4())
