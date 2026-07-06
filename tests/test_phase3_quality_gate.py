"""Tests for Phase 3: quality_gate_node."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_quality_gate_no_obligations():
    """Returns failed with descriptive issue when no obligations exist."""
    from app.agent.nodes.quality_gate import quality_gate_node

    state = {}
    result = await quality_gate_node(state)

    assert result["quality_gate_passed"] is False
    assert len(result["quality_issues"]) > 0
    assert result["current_phase"] == "quality_gate"


@pytest.mark.asyncio
async def test_quality_gate_passes_good_obligations():
    """LLM returns aprobado:true → quality_gate_passed is True."""
    from app.agent.nodes.quality_gate import quality_gate_node

    fake_resp = MagicMock()
    fake_resp.content = json.dumps({
        "aprobado": True,
        "puntuacion": 90,
        "problemas": [],
        "sugerencias": ["Agregar más detalle en cláusula 5"],
    })
    fake_resp.total_tokens = 200

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    obligaciones = [
        {"id": "ob1", "descripcion": "Entregar informe mensual", "plazo": "30 días", "clausula": "5.1"},
        {"id": "ob2", "descripcion": "Asistir a reuniones", "plazo": "semanal", "clausula": "5.2"},
    ]

    with patch("app.agent.nodes.quality_gate.get_llm", return_value=mock_llm):
        result = await quality_gate_node({
            "obligaciones_extraidas": obligaciones,
            "contrato_extraido": {"objeto": "Prestación de servicios de consultoría"},
        })

    assert result["quality_gate_passed"] is True
    assert result["quality_issues"] == []
    assert result["current_phase"] == "quality_gate"


@pytest.mark.asyncio
async def test_quality_gate_fails_bad_obligations():
    """LLM returns aprobado:false → quality_gate_passed is False with issues."""
    from app.agent.nodes.quality_gate import quality_gate_node

    fake_resp = MagicMock()
    fake_resp.content = json.dumps({
        "aprobado": False,
        "puntuacion": 40,
        "problemas": ["Faltan plazos en 3 obligaciones", "No hay referencia a cláusulas"],
        "sugerencias": [],
    })
    fake_resp.total_tokens = 150

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    obligaciones = [
        {"descripcion": "Hacer algo"},
        {"descripcion": "Hacer otra cosa"},
    ]

    with patch("app.agent.nodes.quality_gate.get_llm", return_value=mock_llm):
        result = await quality_gate_node({"obligaciones_extraidas": obligaciones})

    assert result["quality_gate_passed"] is False
    assert len(result["quality_issues"]) == 2
    assert result["current_phase"] == "quality_gate"


@pytest.mark.asyncio
async def test_quality_gate_llm_error_fails_open():
    """When LLM errors, gate passes (fail open) to not block pipeline."""
    from app.agent.nodes.quality_gate import quality_gate_node

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("timeout"))

    obligaciones = [{"descripcion": "Obligación de prueba"}]

    with patch("app.agent.nodes.quality_gate.get_llm", return_value=mock_llm):
        result = await quality_gate_node({"obligaciones_extraidas": obligaciones})

    assert result["quality_gate_passed"] is True
    assert len(result["quality_issues"]) > 0  # has warning message
    assert result["current_phase"] == "quality_gate"


@pytest.mark.asyncio
async def test_quality_gate_invalid_json_from_llm():
    """Handles non-JSON LLM response gracefully."""
    from app.agent.nodes.quality_gate import quality_gate_node

    fake_resp = MagicMock()
    fake_resp.content = "Lo siento, no puedo evaluar estas obligaciones."
    fake_resp.total_tokens = 50

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    with patch("app.agent.nodes.quality_gate.get_llm", return_value=mock_llm):
        result = await quality_gate_node({"obligaciones_extraidas": [{"descripcion": "test"}]})

    assert result["quality_gate_passed"] is False
    assert result["current_phase"] == "quality_gate"


@pytest.mark.asyncio
async def test_quality_gate_wired_in_graph():
    """quality_gate is wired in the compiled graph."""
    from app.agent.graph import build_graph

    g = build_graph()
    assert "quality_gate" in g.nodes
