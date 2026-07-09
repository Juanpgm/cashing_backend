"""Live-LLM test for the obligations extraction node — real Ollama parses a contract excerpt.

The contract text below deliberately avoids every section-header keyword the
deterministic ``extract_obligaciones_verbatim`` parser recognizes (see
app/agent/tools/contract_parser.py — OBLIGACION_SECTION_KW_TIER1/TIER2), so the
node actually escalates to the LLM path instead of short-circuiting on the
regex-based verbatim extractor.
"""

from __future__ import annotations

import pytest

from app.agent.nodes.extraction import obligations_extraction_node

pytestmark = pytest.mark.live_llm

CONTRATO_TEXTO = """\
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES N.° 045-2024

Entre la Secretaría de Desarrollo Social del municipio de Prueba, en adelante
LA ENTIDAD, y Juan Pérez Gómez, identificado con cédula de ciudadanía
n.° 1.020.304.050, en adelante EL PRESTADOR, se suscribe el presente contrato
de prestación de servicios profesionales de apoyo a la gestión.

Valor total del contrato: TREINTA MILLONES DE PESOS ($30.000.000).
Valor mensual: TRES MILLONES DE PESOS ($3.000.000).
Plazo: diez (10) meses, contados desde el 1 de febrero de 2024.

Objeto: prestar servicios profesionales de apoyo a la gestión social,
acompañando la formulación y seguimiento de programas comunitarios en el
municipio.

El presente contrato se rige por lo dispuesto en la Ley 80 de 1993 y demás
normas concordantes. EL PRESTADOR declara conocer las condiciones del
municipio y se compromete a cumplir a cabalidad con las tareas que se
describen a continuación, bajo la supervisión directa de la Secretaría.

Descripción de las tareas a cargo de EL PRESTADOR:

1. Elaborar y presentar informes mensuales de avance sobre los programas
   comunitarios acompañados durante el período de ejecución.
2. Participar en las reuniones de coordinación convocadas por la Secretaría
   con las comunidades beneficiarias del programa.
3. Apoyar la sistematización y el archivo de la documentación social
   generada durante la ejecución del contrato.

Estas tareas se entienden sin perjuicio de las demás que, dentro del marco
del presente contrato, le sean encomendadas por el supervisor designado.

Supervisor: María Rodríguez López, Secretaria de Desarrollo Social.
"""


@pytest.mark.asyncio
async def test_obligations_extraction_from_contract_excerpt() -> None:
    state = {"texto_contrato": CONTRATO_TEXTO}

    result = await obligations_extraction_node(state)

    obligaciones = result["obligaciones_extraidas"]
    assert obligaciones is not None
    assert len(obligaciones) >= 2
    for ob in obligaciones:
        assert isinstance(ob["descripcion"], str)
        assert len(ob["descripcion"].strip()) > 0
