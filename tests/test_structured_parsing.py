"""Unit tests for structured-first (JSON) parsing with regex fallback."""

from __future__ import annotations

from app.agent.tools.contract_parser import (
    obligacion_items_to_extraidas,
    parse_campos_structured,
    parse_obligaciones_structured,
)
from app.schemas.agent import ObligacionItemLLM


class TestParseCamposStructured:
    def test_valid_json_excludes_empty_fields(self) -> None:
        raw = '{"numero_contrato": "CD-1", "objeto": "Algo", "valor_total": "100", "ciudad": ""}'
        result = parse_campos_structured(raw)
        assert result == {"numero_contrato": "CD-1", "objeto": "Algo", "valor_total": "100"}
        assert "ciudad" not in result

    def test_falls_back_to_regex_on_pipe_text(self) -> None:
        raw = "CAMPO|numero_contrato|CD-045-2025\nCAMPO|objeto|Servicios\n"
        result = parse_campos_structured(raw)
        assert result["numero_contrato"] == "CD-045-2025"
        assert result["objeto"] == "Servicios"

    def test_garbage_returns_empty(self) -> None:
        assert parse_campos_structured("not json and not campos") == {}
        assert parse_campos_structured("") == {}


class TestParseObligacionesStructured:
    def test_valid_json_filters_to_especifica(self) -> None:
        raw = (
            '{"obligaciones": ['
            '{"descripcion": "Desarrollar los modulos del sistema", "tipo": "especifica"},'
            '{"descripcion": "Pagar seguridad social", "tipo": "general"}'
            "]}"
        )
        result = parse_obligaciones_structured(raw)
        assert len(result) == 1
        assert result[0].tipo == "especifica"
        assert result[0].descripcion == "Desarrollar los modulos del sistema"

    def test_falls_back_to_regex_on_pipe_text(self) -> None:
        raw = (
            "OBLIGACION|especifica|Disenar los modulos del sistema\n"
            "OBLIGACION|general|Cumplir con el pago de seguridad social\n"
        )
        result = parse_obligaciones_structured(raw)
        assert len(result) == 1
        assert result[0].tipo == "especifica"

    def test_falls_back_to_regex_on_4field_pipe_text(self) -> None:
        """El parser de respaldo acepta el nuevo formato OBLIGACION|tipo|etiqueta|desc."""
        raw = (
            "OBLIGACION|especifica|1|Disenar los modulos del sistema\n"
            "OBLIGACION|especifica|2|Las demas actividades que le sean asignadas por el supervisor\n"
            "OBLIGACION|general||Cumplir con el pago de seguridad social\n"
        )
        result = parse_obligaciones_structured(raw)
        assert len(result) == 2
        assert result[0].etiqueta == "1"
        assert result[1].etiqueta == "2"
        assert "Las demas actividades" in result[1].descripcion

    def test_garbage_returns_empty(self) -> None:
        assert parse_obligaciones_structured("nonsense") == []


class TestObligacionItemsToExtraidas:
    def test_filters_general_and_short_items(self) -> None:
        items = [
            ObligacionItemLLM(descripcion="Desarrollar los modulos", tipo="especifica"),
            ObligacionItemLLM(descripcion="Pagar", tipo="especifica"),  # too short (<=5)
            ObligacionItemLLM(descripcion="Cumplir normas internas", tipo="general"),
            ObligacionItemLLM(descripcion="Realizar las pruebas QA.", tipo="especifica"),
        ]
        result = obligacion_items_to_extraidas(items)
        assert [o.descripcion for o in result] == [
            "Desarrollar los modulos",
            "Realizar las pruebas QA",  # trailing dot stripped
        ]
        assert [o.orden for o in result] == [0, 1]

    def test_etiqueta_propagates_from_llm_item(self) -> None:
        """La etiqueta del ObligacionItemLLM se transfiere al ObligacionExtraida."""
        items = [
            ObligacionItemLLM(descripcion="Disenar la malla curricular del programa tecnico", tipo="especifica", etiqueta="A"),
            ObligacionItemLLM(descripcion="Ejecutar las sesiones de formacion segun calendario", tipo="especifica", etiqueta="B"),
            ObligacionItemLLM(descripcion="Las demas actividades que le sean asignadas por la coordinacion", tipo="especifica", etiqueta="C"),
        ]
        result = obligacion_items_to_extraidas(items)
        assert len(result) == 3
        assert [o.etiqueta for o in result] == ["A", "B", "C"]

    def test_accented_especifica_is_normalized(self) -> None:
        items = [ObligacionItemLLM(descripcion="Presentar informes tecnicos", tipo="específica")]
        result = obligacion_items_to_extraidas(items)
        assert len(result) == 1
        assert result[0].tipo == "especifica"
