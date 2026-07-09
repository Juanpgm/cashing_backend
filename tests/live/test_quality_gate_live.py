"""Live-LLM test for the quality gate node — real Ollama judges extracted obligations.

quality_gate_node hardcodes get_llm(model="gemini/gemini-2.5-flash") — the
autouse `live_llm_settings` fixture (tests/live/conftest.py) monkeypatches this
node's imported `get_llm` reference so it always resolves to the local Ollama
model instead, guaranteeing this test never reaches the cloud.
"""

from __future__ import annotations

import pytest

from app.agent.nodes.quality_gate import quality_gate_node

pytestmark = pytest.mark.live_llm


@pytest.mark.asyncio
async def test_quality_gate_with_good_obligations() -> None:
    obligaciones = [
        {
            "id": "ob1",
            "descripcion": "Elaborar y presentar informes mensuales de avance del programa social",
            "clausula": "3.1",
        },
        {
            "id": "ob2",
            "descripcion": "Participar en las reuniones de coordinación convocadas por la Secretaría",
            "clausula": "3.2",
        },
    ]
    state = {
        "obligaciones_extraidas": obligaciones,
        "contrato_extraido": {"objeto": "Prestación de servicios profesionales de apoyo a la gestión social"},
    }

    result = await quality_gate_node(state)

    # Contract: always a bool verdict + a list of issues + phase marker, never a crash.
    assert isinstance(result["quality_gate_passed"], bool)
    assert isinstance(result["quality_issues"], list)
    assert result["current_phase"] == "quality_gate"
    # Well-formed obligations shouldn't trip the "no obligations" hard-fail path.
    assert result["quality_issues"] != ["No hay obligaciones extraídas para evaluar"]


@pytest.mark.asyncio
async def test_quality_gate_with_garbage_obligations_does_not_crash() -> None:
    """Garbage input must produce a structured verdict, never an exception.

    We deliberately do NOT assert `quality_gate_passed is False` — the node's
    contract only fails open on LLM/parse errors, not on low-quality input; a
    small local model's judgment on "is this garbage" is not guaranteed either
    way, so this is a structural/no-crash assertion.
    """
    state = {"obligaciones_extraidas": ["asdf", "???"]}

    result = await quality_gate_node(state)

    assert isinstance(result["quality_gate_passed"], bool)
    assert isinstance(result["quality_issues"], list)
    assert result["current_phase"] == "quality_gate"
