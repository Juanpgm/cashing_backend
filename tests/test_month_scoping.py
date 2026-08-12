"""Table-tests for the month-scoping window helper (`app.core.month_scoping`).

Pure function, no I/O, no DB. See design.md ("radicacion-stepper") section 4 for the
committed window rule:

    fecha_inicio = first calendar day of (mes, anio)
    end_of_month = last calendar day of (mes, anio)

    if fecha_transaccion is set and fecha_transaccion >= fecha_inicio:
        fecha_fin = min(end_of_month, fecha_transaccion)
    else:
        fecha_fin = end_of_month  # + soft-warning flag when fecha_transaccion < fecha_inicio
"""

from __future__ import annotations

from datetime import date

import pytest
from app.core.month_scoping import calcular_ventana_mes


class TestCalcularVentanaMes:
    def test_fecha_transaccion_omitida_usa_mes_calendario_completo(self) -> None:
        """No fecha_transaccion -> full calendar month, no warning."""
        ventana = calcular_ventana_mes(mes=4, anio=2025, fecha_transaccion=None)

        assert ventana.fecha_inicio == date(2025, 4, 1)
        assert ventana.fecha_fin == date(2025, 4, 30)
        assert ventana.advertencia is False

    def test_fecha_transaccion_dentro_del_mes_acota_el_fin(self) -> None:
        """fecha_transaccion within the month caps fecha_fin to that date."""
        ventana = calcular_ventana_mes(mes=4, anio=2025, fecha_transaccion=date(2025, 4, 15))

        assert ventana.fecha_inicio == date(2025, 4, 1)
        assert ventana.fecha_fin == date(2025, 4, 15)
        assert ventana.advertencia is False

    def test_fecha_transaccion_igual_a_fecha_inicio_es_el_borde_inclusivo(self) -> None:
        """fecha_transaccion == fecha_inicio satisfies the `>=` branch (inclusive)."""
        ventana = calcular_ventana_mes(mes=4, anio=2025, fecha_transaccion=date(2025, 4, 1))

        assert ventana.fecha_inicio == date(2025, 4, 1)
        assert ventana.fecha_fin == date(2025, 4, 1)
        assert ventana.advertencia is False

    def test_fecha_transaccion_posterior_al_fin_de_mes_se_limita_al_fin_de_mes(self) -> None:
        """fecha_transaccion beyond end_of_month is capped by min() -> end_of_month wins."""
        ventana = calcular_ventana_mes(mes=4, anio=2025, fecha_transaccion=date(2025, 5, 10))

        assert ventana.fecha_inicio == date(2025, 4, 1)
        assert ventana.fecha_fin == date(2025, 4, 30)
        assert ventana.advertencia is False

    def test_fecha_transaccion_anterior_a_fecha_inicio_cae_a_mes_completo_con_advertencia(self) -> None:
        """Misconfiguration edge case: fecha_transaccion < fecha_inicio -> full month + soft warning."""
        ventana = calcular_ventana_mes(mes=4, anio=2025, fecha_transaccion=date(2025, 3, 20))

        assert ventana.fecha_inicio == date(2025, 4, 1)
        assert ventana.fecha_fin == date(2025, 4, 30)
        assert ventana.advertencia is True

    def test_mes_de_febrero_bisiesto_calcula_29_dias(self) -> None:
        """Sanity check: variable month-length (leap year Feb) is computed correctly, not hardcoded."""
        ventana = calcular_ventana_mes(mes=2, anio=2024, fecha_transaccion=None)

        assert ventana.fecha_inicio == date(2024, 2, 1)
        assert ventana.fecha_fin == date(2024, 2, 29)
        assert ventana.advertencia is False

    def test_mes_de_febrero_no_bisiesto_calcula_28_dias(self) -> None:
        ventana = calcular_ventana_mes(mes=2, anio=2025, fecha_transaccion=None)

        assert ventana.fecha_inicio == date(2025, 2, 1)
        assert ventana.fecha_fin == date(2025, 2, 28)
        assert ventana.advertencia is False

    @pytest.mark.parametrize(
        ("mes", "anio", "fecha_transaccion", "esperado_fin", "esperado_advertencia"),
        [
            (1, 2025, None, date(2025, 1, 31), False),
            (6, 2025, date(2025, 6, 1), date(2025, 6, 1), False),
            (6, 2025, date(2025, 6, 30), date(2025, 6, 30), False),
            (6, 2025, date(2025, 7, 1), date(2025, 6, 30), False),
            (6, 2025, date(2025, 5, 31), date(2025, 6, 30), True),
            (12, 2025, None, date(2025, 12, 31), False),
        ],
    )
    def test_tabla_de_casos(
        self,
        mes: int,
        anio: int,
        fecha_transaccion: date | None,
        esperado_fin: date,
        esperado_advertencia: bool,
    ) -> None:
        ventana = calcular_ventana_mes(mes=mes, anio=anio, fecha_transaccion=fecha_transaccion)

        assert ventana.fecha_fin == esperado_fin
        assert ventana.advertencia is esperado_advertencia


class TestVentanaVigenciaContrato:
    """H5: optional contract vigencia — advertencia=True when the billed calendar
    month falls ENTIRELY outside [vigencia_inicio, vigencia_fin]. Warn-only."""

    VIG_INI = date(2024, 1, 15)
    VIG_FIN = date(2024, 12, 15)

    def test_mes_despues_de_vigencia_advierte(self) -> None:
        ventana = calcular_ventana_mes(mes=4, anio=2025, vigencia_inicio=self.VIG_INI, vigencia_fin=self.VIG_FIN)
        assert ventana.advertencia is True
        # Window itself is unchanged — vigencia only drives the flag.
        assert ventana.fecha_inicio == date(2025, 4, 1)
        assert ventana.fecha_fin == date(2025, 4, 30)

    def test_mes_antes_de_vigencia_advierte(self) -> None:
        ventana = calcular_ventana_mes(mes=12, anio=2023, vigencia_inicio=self.VIG_INI, vigencia_fin=self.VIG_FIN)
        assert ventana.advertencia is True

    def test_mes_dentro_de_vigencia_no_advierte(self) -> None:
        ventana = calcular_ventana_mes(mes=6, anio=2024, vigencia_inicio=self.VIG_INI, vigencia_fin=self.VIG_FIN)
        assert ventana.advertencia is False

    def test_mes_parcialmente_dentro_no_advierte(self) -> None:
        """Vigencia starts/ends mid-month: any overlap with the month is enough."""
        assert (
            calcular_ventana_mes(mes=1, anio=2024, vigencia_inicio=self.VIG_INI, vigencia_fin=self.VIG_FIN).advertencia
            is False
        )
        assert (
            calcular_ventana_mes(mes=12, anio=2024, vigencia_inicio=self.VIG_INI, vigencia_fin=self.VIG_FIN).advertencia
            is False
        )

    def test_sin_vigencia_comportamiento_intacto(self) -> None:
        ventana = calcular_ventana_mes(mes=4, anio=2025, fecha_transaccion=date(2025, 4, 15))
        assert ventana.advertencia is False

    def test_fuera_de_vigencia_con_fecha_transaccion_dentro_del_mes(self) -> None:
        """The fecha_transaccion cap branch must also surface the vigencia warning."""
        ventana = calcular_ventana_mes(
            mes=4,
            anio=2025,
            fecha_transaccion=date(2025, 4, 15),
            vigencia_inicio=self.VIG_INI,
            vigencia_fin=self.VIG_FIN,
        )
        assert ventana.advertencia is True
        assert ventana.fecha_fin == date(2025, 4, 15)
