"""Spell out contract amounts and cuota ordinals in Spanish (Colombian billing style)."""

from __future__ import annotations

from decimal import Decimal

from num2words import num2words

# Feminine ordinals for cuota numbering ("primera cuota", ...). num2words only
# emits masculine forms, so the common range is mapped by hand.
_ORDINALES_FEMENINOS = {
    1: "primera",
    2: "segunda",
    3: "tercera",
    4: "cuarta",
    5: "quinta",
    6: "sexta",
    7: "séptima",
    8: "octava",
    9: "novena",
    10: "décima",
    11: "undécima",
    12: "duodécima",
}


def valor_en_letras(valor: Decimal | int) -> str:
    """Whole-peso amount in words, e.g. 4800000 → "CUATRO MILLONES OCHOCIENTOS MIL PESOS M/CTE".

    Cents are ignored (these contracts use whole pesos); a Decimal with
    decimals contributes only its integer part.
    """
    letras: str = num2words(int(valor), lang="es")
    return f"{letras.upper()} PESOS M/CTE"


def ordinal_letras(n: int) -> str:
    """Feminine ordinal for cuota n: 1 → "primera", 2 → "segunda"; fallback "cuota {n}"."""
    return _ORDINALES_FEMENINOS.get(n, f"cuota {n}")
