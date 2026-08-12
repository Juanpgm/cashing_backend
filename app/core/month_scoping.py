"""Pure date-window computation for month-scoped pipelines (evidence discovery, justification).

No I/O — the caller resolves ``mes``/``anio``/``fecha_transaccion`` from the DB; this module
only computes the bounding window. See design.md ("radicacion-stepper") section 4 for the
committed rule and its misconfiguration edge case.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class VentanaMes:
    """Bounding date-window for a billed month, optionally capped by ``fecha_transaccion``."""

    fecha_inicio: date
    fecha_fin: date
    # True when fecha_transaccion < fecha_inicio (misconfiguration fallback) OR when the
    # billed calendar month falls entirely outside the contract vigencia (H5, warn-only).
    advertencia: bool


def calcular_ventana_mes(
    mes: int,
    anio: int,
    fecha_transaccion: date | None = None,
    vigencia_inicio: date | None = None,
    vigencia_fin: date | None = None,
) -> VentanaMes:
    """Calendar-month window for ``(mes, anio)``, upper-capped by ``fecha_transaccion`` when present.

    Rule:
    - Base = calendar month of ``(mes, anio)`` — not a rolling window.
    - ``fecha_transaccion`` caps the upper bound (``min(end_of_month, fecha_transaccion)``)
      when it falls on/after the first day of the month.
    - ``fecha_transaccion < fecha_inicio`` (misconfiguration) falls back to the full
      calendar month and sets ``advertencia=True`` instead of producing an empty/invalid
      window. Omitting ``fecha_transaccion`` entirely never triggers the warning.
    - ``vigencia_inicio``/``vigencia_fin`` (contract vigencia, optional): when the billed
      calendar month falls ENTIRELY outside ``[vigencia_inicio, vigencia_fin]``,
      ``advertencia=True`` too (H5: warn, never reject — the caller decides what to do).
      Omitting them keeps the previous behavior unchanged.
    """
    fecha_inicio = date(anio, mes, 1)
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    fin_de_mes = date(anio, mes, ultimo_dia)

    fuera_de_vigencia = (vigencia_inicio is not None and fin_de_mes < vigencia_inicio) or (
        vigencia_fin is not None and fecha_inicio > vigencia_fin
    )

    if fecha_transaccion is not None and fecha_transaccion >= fecha_inicio:
        return VentanaMes(
            fecha_inicio=fecha_inicio,
            fecha_fin=min(fin_de_mes, fecha_transaccion),
            advertencia=fuera_de_vigencia,
        )

    advertencia = (fecha_transaccion is not None and fecha_transaccion < fecha_inicio) or fuera_de_vigencia
    return VentanaMes(fecha_inicio=fecha_inicio, fecha_fin=fin_de_mes, advertencia=advertencia)
