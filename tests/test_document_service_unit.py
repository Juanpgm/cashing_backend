"""Tests for document_service pure helper functions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


class TestSafeDecimal:
    def test_none_returns_zero(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal(None) == Decimal("0.00")

    def test_empty_returns_zero(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal("") == Decimal("0.00")

    def test_plain_integer_string(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal("1000000") == Decimal("1000000")

    def test_dollar_sign_removed(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal("$3500000") == Decimal("3500000")

    def test_space_removed(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal("1 000 000") == Decimal("1000000")

    def test_colombian_format_dot_thousands_comma_decimal(self) -> None:
        """1.500.000,50 → 1500000.50"""
        from app.services.document_service import _safe_decimal
        result = _safe_decimal("1.500.000,50")
        assert result == Decimal("1500000.50")

    def test_comma_as_thousands_separator(self) -> None:
        """1,500,000 → 1500000"""
        from app.services.document_service import _safe_decimal
        result = _safe_decimal("1,500,000")
        assert result == Decimal("1500000")

    def test_comma_as_decimal_separator(self) -> None:
        """35,50 → 35.50"""
        from app.services.document_service import _safe_decimal
        result = _safe_decimal("35,50")
        assert result == Decimal("35.50")

    def test_invalid_returns_zero(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal("not-a-number") == Decimal("0.00")

    def test_float_string(self) -> None:
        from app.services.document_service import _safe_decimal
        assert _safe_decimal("3500000.50") == Decimal("3500000.50")


class TestSafeDate:
    def test_none_returns_none(self) -> None:
        from app.services.document_service import _safe_date
        assert _safe_date(None) is None

    def test_empty_returns_none(self) -> None:
        from app.services.document_service import _safe_date
        assert _safe_date("") is None

    def test_valid_iso_date(self) -> None:
        from app.services.document_service import _safe_date
        assert _safe_date("2024-01-15") == date(2024, 1, 15)

    def test_with_surrounding_spaces(self) -> None:
        from app.services.document_service import _safe_date
        assert _safe_date("  2024-06-30  ") == date(2024, 6, 30)

    def test_invalid_format_returns_none(self) -> None:
        from app.services.document_service import _safe_date
        assert _safe_date("15/01/2024") is None


class TestBuildContratoExtraido:
    def test_returns_none_when_no_numero_and_no_objeto(self) -> None:
        from app.services.document_service import _build_contrato_extraido
        result = _build_contrato_extraido({})
        assert result is None

    def test_builds_with_only_numero(self) -> None:
        from app.services.document_service import _build_contrato_extraido
        result = _build_contrato_extraido({"numero_contrato": "CO1.PCCNTR.123"})
        assert result is not None
        assert result.numero_contrato == "CO1.PCCNTR.123"

    def test_builds_with_only_objeto(self) -> None:
        from app.services.document_service import _build_contrato_extraido
        result = _build_contrato_extraido({"objeto": "Servicios de consultoría"})
        assert result is not None
        assert result.numero_contrato == "SIN-NUMERO"

    def test_builds_full_contrato(self) -> None:
        from app.services.document_service import _build_contrato_extraido
        campos = {
            "numero_contrato": "CO1.PCCNTR.456",
            "objeto": "Consultoría TI",
            "valor_total": "5000000",
            "valor_mensual": "1000000",
            "fecha_inicio": "2024-01-01",
            "fecha_fin": "2024-12-31",
            "entidad": "MinTIC",
        }
        result = _build_contrato_extraido(campos)
        assert result is not None
        assert result.valor_total == Decimal("5000000")
        assert result.fecha_inicio == date(2024, 1, 1)
        assert result.entidad == "MinTIC"
