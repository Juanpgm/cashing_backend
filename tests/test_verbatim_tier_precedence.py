"""Section scoring must prefer a tier-1 heading over a longer tier-2 list.

`extract_obligaciones_verbatim` scores every candidate section instead of taking
the first match. The score tuple was ``(catch_all, len(items), tier == 1)``, so
item COUNT outranked the heading tier: a "CLÁUSULA — OBLIGACIONES DEL
CONTRATISTA" block (tier 2 — general duties mixed in) with 12 items beat the real
"OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA" enumeration with 5, and the contract's
general duties were persisted as its specific obligations.

Tier is the strongest signal available and must come first:
``(tier == 1, catch_all, len(items))``.
"""

from __future__ import annotations

from app.agent.tools.contract_parser import extract_obligaciones_verbatim

_GENERALES = [
    "Cumplir con el objeto del contrato en los términos pactados por las partes",
    "Obrar con lealtad y buena fe en las distintas etapas contractuales",
    "Afiliarse y cotizar al sistema general de seguridad social integral",
    "Presentar los informes de actividades que le sean solicitados",
    "Mantener vigentes las garantías exigidas durante la ejecución",
    "Guardar la reserva sobre la información que conozca por el contrato",
    "Responder por sus actuaciones y omisiones frente a terceros",
    "Atender los requerimientos de los organismos de control",
    "Reportar oportunamente cualquier situación que afecte la ejecución",
    "Devolver los elementos entregados para el desarrollo de sus funciones",
    "Acatar la Constitución, la ley y los reglamentos internos de la entidad",
    "Las demás que le sean asignadas por la ley y por el supervisor del contrato",
]

_ESPECIFICAS = [
    "Diseñar e implementar los módulos del sistema de información SISBEN IV",
    "Realizar las pruebas unitarias y de integración de cada componente",
    "Capacitar a los funcionarios de la entidad en el uso del nuevo sistema",
    "Documentar los procedimientos técnicos y los manuales de usuario",
    "Las demás actividades que le asigne la supervisión relacionadas con el objeto",
]


def _enumerar(items: list[str]) -> str:
    return "\n".join(f"{i}. {texto}." for i, texto in enumerate(items, start=1))


TEXTO = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0456

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

CLÁUSULA SEGUNDA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar(_GENERALES)}

CLÁUSULA TERCERA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

{_enumerar(_ESPECIFICAS)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""


class TestTierBeatsItemCount:
    def test_tier1_section_with_five_items_beats_tier2_section_with_twelve(self) -> None:
        obligaciones = extract_obligaciones_verbatim(TEXTO)

        descripciones = [o.descripcion for o in obligaciones]
        assert len(descripciones) == len(_ESPECIFICAS), (
            f"expected the tier-1 ESPECÍFICAS section, got {len(descripciones)} items: {descripciones}"
        )
        assert descripciones[0].startswith("Diseñar e implementar"), descripciones
        assert not any("lealtad y buena fe" in d for d in descripciones), (
            f"general duties leaked into the specific obligations: {descripciones}"
        )


# Counter-example from the round-2 review (engram obs #492): a PARÁGRAFO merely
# MENTIONING "obligaciones específicas" in prose and closing with a single
# catch-all item is tier-1 by keyword match alone, but it is not a real
# enumeration. Scoring tier ahead of everything else let this 1-item tier-1
# block beat the real 12-item tier-2 "OBLIGACIONES DEL CONTRATISTA" list.
TEXTO_PARAGRAFO_MENCION = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0789

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

CLÁUSULA SEGUNDA — PARÁGRAFO: para los efectos de este contrato,
las obligaciones específicas del contratista consisten en lo siguiente:

1. Cumplir con las demás actividades inherentes al objeto contractual.

CLÁUSULA TERCERA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar(_GENERALES)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""


class TestTier1NeedsAtLeastTwoItemsToOutrankTier2:
    def test_single_item_tier1_paragrafo_does_not_beat_real_tier2_list(self) -> None:
        obligaciones = extract_obligaciones_verbatim(TEXTO_PARAGRAFO_MENCION)

        descripciones = [o.descripcion for o in obligaciones]
        assert len(descripciones) == len(_GENERALES), (
            f"expected the real tier-2 12-item list, got {len(descripciones)} items: {descripciones}"
        )
        assert descripciones[0].startswith("Cumplir con el objeto"), descripciones
