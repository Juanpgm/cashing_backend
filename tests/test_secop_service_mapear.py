"""Tests for secop_service pure helper functions: _calcular_valor_mensual, _mapear_a_contrato_create."""

from __future__ import annotations

from datetime import date
from decimal import Decimal


class TestCalcularValorMensual:
    def test_one_month_contract(self) -> None:
        from app.services.secop_service import _calcular_valor_mensual

        valor = Decimal("3000000")
        inicio = date(2024, 1, 1)
        fin = date(2024, 2, 1)  # ~31 days → 1 month
        result = _calcular_valor_mensual(valor, inicio, fin)
        assert result == Decimal("3000000.00")

    def test_twelve_month_contract(self) -> None:
        from app.services.secop_service import _calcular_valor_mensual

        valor = Decimal("36000000")
        inicio = date(2024, 1, 1)
        fin = date(2025, 1, 1)  # 365 days → ~12 months
        result = _calcular_valor_mensual(valor, inicio, fin)
        assert result == Decimal("3000000.00")

    def test_zero_days_returns_total(self) -> None:
        """Edge case: same start and end date → 0 days → max(1,0)=1 month."""
        from app.services.secop_service import _calcular_valor_mensual

        valor = Decimal("5000000")
        d = date(2024, 6, 1)
        result = _calcular_valor_mensual(valor, d, d)
        assert result == valor.quantize(Decimal("0.01"))


class TestMapearAContratoCreate:
    def _make_row(self, **kwargs) -> dict:
        defaults = {
            "numero_contrato": "CO1.PCCNTR.001",
            "objeto_del_contrato": "Servicios de consultoría en tecnologías de la información",
            "valor_del_contrato": "5000000",
            "fecha_de_inicio_del_contrato": "2024-01-01T00:00:00.000",
            "fecha_de_fin_del_contrato": "2024-12-31T00:00:00.000",
            "nombre_entidad": "MinTIC",
        }
        defaults.update(kwargs)
        return defaults

    def test_valid_row_returns_contrato_create(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        result = _mapear_a_contrato_create(self._make_row())
        assert result is not None
        assert result.numero_contrato == "CO1.PCCNTR.001"
        assert result.entidad == "MinTIC"

    def test_missing_numero_contrato_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(numero_contrato="", referencia_del_contrato="", id_contrato="")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_short_objeto_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(objeto_del_contrato="Corto")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_zero_valor_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(valor_del_contrato="0")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_negative_valor_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(valor_del_contrato="-100")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_invalid_valor_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(valor_del_contrato="no-es-numero")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_missing_dates_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(fecha_de_inicio_del_contrato=None, fecha_de_fin_del_contrato=None)
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_end_before_start_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(
            fecha_de_inicio_del_contrato="2024-12-31T00:00:00.000",
            fecha_de_fin_del_contrato="2024-01-01T00:00:00.000",
        )
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_uses_referencia_del_contrato_as_fallback(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(numero_contrato="", referencia_del_contrato="CO1.REF.999")
        result = _mapear_a_contrato_create(row)
        assert result is not None
        assert result.numero_contrato == "CO1.REF.999"

    def test_supervisor_is_mapped(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(nombre_supervisor="Juan Supervisor")
        result = _mapear_a_contrato_create(row)
        assert result is not None
        assert result.supervisor_nombre == "Juan Supervisor"

    def test_calculates_valor_mensual(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = self._make_row(valor_del_contrato="12000000")
        result = _mapear_a_contrato_create(row)
        assert result is not None
        # 364 days ≈ 12 months → ~1000000/month
        assert result.valor_mensual > Decimal("0")
