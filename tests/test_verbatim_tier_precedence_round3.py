"""Counter-examples that a bullet-COUNT threshold can never separate.

Round-3 review (engram obs #495, BLOCKER 2) reproduced three failures of the
``(tier == 1 and len(items) >= 2, catch_all, len(items))`` score against the
real extractor:

(a) The PARÁGRAFO prose mention with TWO throwaway bullets clears the ``>= 2``
    guard and beats the real 12-item tier-2 list. The threshold moved by
    exactly one.
(b) A GENUINE single-obligation ESPECÍFICAS clause fails the ``>= 2`` guard and
    loses to the generic 12-item tier-2 clause — general duties persisted as
    specific obligations, the exact class the scoring exists to kill.
(c) Tier-1 vs tier-1: two candidates tie at the first tuple element, so the
    decision falls to ``catch_all`` and a 2-item prose block ending in "las
    demás" beats a REAL 12-item ``OBLIGACIONES ESPECÍFICAS`` enumeration.

(a) and (b) pull the threshold in opposite directions, and (c) cannot be
decided by ANY threshold — the guard only demotes tier-1 to tier-2, it cannot
order two tier-1 candidates. The discriminator has to be structural: whether
the keyword occurrence is a real UPPERCASE section heading or a lowercase prose
mention.
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

_ESPECIFICAS_LARGAS = [
    "Diseñar e implementar los módulos del sistema de información SISBEN IV",
    "Realizar las pruebas unitarias y de integración de cada componente",
    "Capacitar a los funcionarios de la entidad en el uso del nuevo sistema",
    "Documentar los procedimientos técnicos y los manuales de usuario",
    "Migrar la información histórica desde la plataforma actualmente en uso",
    "Configurar los ambientes de desarrollo, pruebas y producción del sistema",
    "Definir el modelo de datos y los diccionarios de las entidades del dominio",
    "Implementar los tableros de control solicitados por la subdirección",
    "Atender los incidentes reportados por la mesa de servicio de la entidad",
    "Elaborar el plan de despliegue y el protocolo de reversión del sistema",
    "Realizar las pruebas de carga y el ajuste de rendimiento de la solución",
    "Entregar el código fuente y los artefactos de despliegue a la entidad",
]


def _enumerar(items: list[str]) -> str:
    return "\n".join(f"{i}. {texto}." for i, texto in enumerate(items, start=1))


# (a) The round-2 PARÁGRAFO prose mention, now with TWO throwaway bullets —
# enough to clear a `>= 2` guard while still being prose, not an enumeration.
TEXTO_A_PARAGRAFO_DOS_BULLETS = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0789

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

CLÁUSULA SEGUNDA — PARÁGRAFO: para los efectos de este contrato,
las obligaciones específicas del contratista consisten en lo siguiente:

1. Atender lo previsto en el manual de contratación de la entidad.
2. Cumplir con las demás actividades inherentes al objeto contractual.

CLÁUSULA TERCERA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar(_GENERALES)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""

# (b) A genuine ESPECÍFICAS clause that happens to carry a SINGLE obligation.
# The contract really does list one specific duty; it must still win over the
# generic 12-item clause.
_ESPECIFICA_UNICA = ["Diseñar e implementar los módulos del sistema de información SISBEN IV"]

TEXTO_B_ESPECIFICA_UNICA = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0321

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

CLÁUSULA SEGUNDA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar(_GENERALES)}

CLÁUSULA TERCERA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

{_enumerar(_ESPECIFICA_UNICA)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""

# (c) Tier-1 vs tier-1. The prose block comes FIRST in the document (so the
# "first candidate keeps a tie" rule cannot rescue the real one) and closes with
# a catch-all, which is exactly what the old score rewarded.
TEXTO_C_PROSA_TIER1_VS_ENCABEZADO_TIER1 = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0654

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

CLÁUSULA SEGUNDA — PARÁGRAFO: se entiende que las obligaciones específicas
del contratista se ejecutarán conforme a lo siguiente:

1. Atender lo previsto en el manual de contratación de la entidad.
2. Las demás actividades que le asigne la supervisión del contrato.

CLÁUSULA TERCERA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

{_enumerar(_ESPECIFICAS_LARGAS)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""


class TestProseMentionNeverBeatsARealHeading:
    def test_a_paragrafo_prose_mention_with_two_bullets_loses_to_the_real_tier2_list(self) -> None:
        descripciones = [o.descripcion for o in extract_obligaciones_verbatim(TEXTO_A_PARAGRAFO_DOS_BULLETS)]

        assert len(descripciones) == len(_GENERALES), (
            f"expected the real tier-2 12-item list, got {len(descripciones)}: {descripciones}"
        )
        assert descripciones[0].startswith("Cumplir con el objeto"), descripciones
        assert not any("manual de contratación" in d for d in descripciones), (
            f"the PARÁGRAFO prose bullets leaked in: {descripciones}"
        )

    def test_c_prose_tier1_block_loses_to_a_real_tier1_heading(self) -> None:
        descripciones = [
            o.descripcion for o in extract_obligaciones_verbatim(TEXTO_C_PROSA_TIER1_VS_ENCABEZADO_TIER1)
        ]

        assert len(descripciones) == len(_ESPECIFICAS_LARGAS), (
            f"expected the real ESPECÍFICAS enumeration, got {len(descripciones)}: {descripciones}"
        )
        assert descripciones[0].startswith("Diseñar e implementar"), descripciones
        assert not any("manual de contratación" in d for d in descripciones), (
            f"the prose block's catch-all outranked a real 12-item heading: {descripciones}"
        )


class TestARealHeadingWinsRegardlessOfItemCount:
    def test_b_genuine_single_obligacion_especificas_beats_the_generic_twelve(self) -> None:
        descripciones = [o.descripcion for o in extract_obligaciones_verbatim(TEXTO_B_ESPECIFICA_UNICA)]

        assert len(descripciones) == 1, (
            f"expected the genuine 1-item ESPECÍFICAS clause, got {len(descripciones)}: {descripciones}"
        )
        assert descripciones[0].startswith("Diseñar e implementar"), descripciones
        assert not any("lealtad y buena fe" in d for d in descripciones), (
            f"general duties persisted as specific obligations: {descripciones}"
        )
