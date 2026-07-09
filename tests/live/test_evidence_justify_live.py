"""Live-LLM test for the evidence_justify node — real Ollama drafts actividad + justificacion."""

from __future__ import annotations

import pytest

from app.agent.nodes.evidence_justify import evidence_justify_node
from app.agent.prompts.actividad_generation import is_near_identical

pytestmark = pytest.mark.live_llm


@pytest.mark.asyncio
async def test_evidence_justify_with_gmail_style_evidence() -> None:
    obligacion_texto = "Entregar informe mensual de avance a la supervisión del contrato"
    state = {
        "obligaciones_contexto": [{"id": "ob1", "descripcion": obligacion_texto}],
        "matched_evidence": {
            "ob1": [
                {
                    "source": "email",
                    "title": "Informe de avance mensual - abril 2024",
                    "link": "https://mail.google.com/mail/u/0/#inbox/xyz123",
                    "date": "2024-04-28",
                },
                {
                    "source": "email",
                    "title": "Confirmación reunión de seguimiento con supervisor",
                    "link": "https://mail.google.com/mail/u/0/#inbox/abc456",
                    "date": "2024-04-15",
                },
            ]
        },
        "contrato_contexto": {"objeto": "Prestación de servicios profesionales de apoyo a la gestión social"},
    }

    result = await evidence_justify_node(state)

    justificaciones = result["justificaciones"]
    assert len(justificaciones) == 1
    entry = justificaciones[0]

    actividad = entry["actividad"]
    justificacion = entry["justificacion"]

    assert actividad
    assert justificacion
    assert not is_near_identical(actividad, justificacion)
    assert actividad.strip() != obligacion_texto
    assert justificacion.strip() != obligacion_texto
    assert len(entry["evidencias"]) == 2
