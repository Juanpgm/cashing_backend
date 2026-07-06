"""Tests for the evidence_justify node and the extended evidence_orchestrator merge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# evidence_orchestrator — drive + calendar merge (extended)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_merges_four_sources():
    from app.agent.nodes.evidence_orchestrator import evidence_orchestrator_node

    state = {
        "email_evidencias": [{"source": "email", "content": "correo", "subject": "Acta", "link": "g1"}],
        "drive_evidencias": [{"source": "drive", "title": "informe.pdf", "content": "informe", "link": "d1"}],
        "calendar_evidencias": [{"source": "calendar", "title": "Reunión", "content": "reunión", "link": "c1"}],
        "local_evidence": [{"filename": "soporte.pdf", "text": "soporte"}],
    }
    result = await evidence_orchestrator_node(state)

    sources = {e["source"] for e in result["evidence_raw"]}
    assert sources == {"email", "drive", "calendar", "local_file"}
    drive_ev = next(e for e in result["evidence_raw"] if e["source"] == "drive")
    assert drive_ev["link"] == "d1"


# ─────────────────────────────────────────────────────────────────────────────
# evidence_justify_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_justify_generates_text_and_links():
    from app.agent.nodes import evidence_justify as mod

    fake_resp = MagicMock()
    fake_resp.content = "Durante el período realicé las actividades soportadas en las evidencias."
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual"}],
        "matched_evidence": {
            "ob1": [
                {"source": "drive", "title": "informe.pdf", "link": "https://drive/x", "date": "2024-04-10"},
            ]
        },
    }

    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)

    just = result["justificaciones"]
    assert len(just) == 1
    assert just[0]["obligacion_id"] == "ob1"
    assert "actividades" in just[0]["justificacion"]
    assert just[0]["evidencias"][0]["link"] == "https://drive/x"
    assert just[0]["evidencias"][0]["titulo"] == "informe.pdf"


@pytest.mark.asyncio
async def test_evidence_justify_no_evidence_uses_fallback():
    from app.agent.nodes import evidence_justify as mod

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("llm down"))

    state = {
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones"}],
        "matched_evidence": {"ob1": []},
    }

    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)

    assert result["justificaciones"][0]["evidencias"] == []
    assert "No se encontraron evidencias" in result["justificaciones"][0]["justificacion"]
