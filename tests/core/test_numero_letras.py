"""Tests for numero_letras (amounts and cuota ordinals in Spanish words)."""

from __future__ import annotations

from decimal import Decimal

from app.core.numero_letras import ordinal_letras, valor_en_letras
from num2words import num2words


class TestValorEnLetras:
    def test_exact_values(self) -> None:
        assert valor_en_letras(100) == "CIEN PESOS M/CTE"
        assert valor_en_letras(500) == "QUINIENTOS PESOS M/CTE"
        assert valor_en_letras(1_000_000) == "UN MILLÓN PESOS M/CTE"
        assert valor_en_letras(1_500_000) == "UN MILLÓN QUINIENTOS MIL PESOS M/CTE"

    def test_21_follows_num2words_spelling(self) -> None:
        # Don't hardcode disputable orthography — trust num2words' own spelling.
        esperado = num2words(21, lang="es").upper()
        assert valor_en_letras(21) == f"{esperado} PESOS M/CTE"

    def test_millions_from_decimal(self) -> None:
        resultado = valor_en_letras(Decimal("4800000"))
        assert resultado == "CUATRO MILLONES OCHOCIENTOS MIL PESOS M/CTE"
        assert "MILLONES" in resultado
        assert resultado.endswith("PESOS M/CTE")

    def test_decimals_ignore_cents(self) -> None:
        assert valor_en_letras(Decimal("100.99")) == "CIEN PESOS M/CTE"


class TestOrdinalLetras:
    def test_feminine_ordinals(self) -> None:
        assert ordinal_letras(1) == "primera"
        assert ordinal_letras(2) == "segunda"
        assert ordinal_letras(3) == "tercera"
        assert ordinal_letras(12) == "duodécima"

    def test_fallback_beyond_mapping(self) -> None:
        assert ordinal_letras(25) == "cuota 25"
