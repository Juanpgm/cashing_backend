"""Live-LLM test for the activities generation node — real Ollama drafts billing activities."""

from __future__ import annotations

import pytest

from app.agent.nodes.activities import generate_activities_node

pytestmark = pytest.mark.live_llm


@pytest.mark.asyncio
async def test_generate_activities_for_two_obligations() -> None:
    obligaciones = [
        {"descripcion": "Entregar informe mensual de avance a la supervisión del contrato"},
        {"descripcion": "Asistir a reuniones periódicas de seguimiento con el equipo técnico"},
    ]
    state = {
        "obligaciones_contexto": obligaciones,
        "contrato_contexto": {
            "numero_contrato": "123-2024",
            "entidad": "Alcaldía de Prueba",
            "objeto": "Prestación de servicios profesionales de apoyo técnico",
        },
        "mes": 5,
        "anio": 2024,
    }

    result = await generate_activities_node(state)

    actividades = result["actividades_generadas"]
    # A small local model won't always produce one line per obligación, but it
    # must produce at least one well-formed activity.
    assert len(actividades) >= 1

    obligacion_texts = {ob["descripcion"] for ob in obligaciones}
    for act in actividades:
        assert set(act.keys()) >= {"descripcion", "justificacion", "obligacion_orden"}
        assert isinstance(act["descripcion"], str)
        assert len(act["descripcion"]) >= 10
        # Non-echo: the generated activity must not just be the obligación's own text.
        assert act["descripcion"] not in obligacion_texts
        assert isinstance(act["obligacion_orden"], int)
