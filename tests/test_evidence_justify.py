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
    assert just[0]["origen"] == "llm"


@pytest.mark.asyncio
async def test_evidence_justify_no_evidence_uses_sentinel_without_llm_call():
    """Zero evidencias for the period → deterministic sentinel verbatim, LLM never invoked
    (previously the LLM was called anyway and produced useless meta-text about the
    absence of evidence)."""
    from app.agent.nodes import evidence_justify as mod

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=AssertionError("LLM must not be called with 0 evidencias"))

    state = {
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones"}],
        "matched_evidence": {"ob1": []},
    }

    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)

    assert result["justificaciones"][0]["evidencias"] == []
    assert result["justificaciones"][0]["justificacion"] == mod.TEXTO_SIN_LABORES
    assert result["justificaciones"][0]["origen"] == "sin_labores"
    mock_llm.complete.assert_not_called()
    # Fallback actividad must never echo the obligación's own text.
    assert result["justificaciones"][0]["actividad"] != "Asistir a reuniones"
    assert result["justificaciones"][0]["actividad"] != result["justificaciones"][0]["justificacion"]


@pytest.mark.asyncio
async def test_evidence_justify_parses_strict_actividad_justificacion_format():
    """When the LLM follows the ACTIVIDAD:/JUSTIFICACION: contract, both fields are
    parsed out distinctly (not both set to the same raw response text)."""
    from app.agent.nodes import evidence_justify as mod

    fake_resp = MagicMock()
    fake_resp.content = (
        "ACTIVIDAD: Elaboré y entregué el informe mensual de avance al supervisor.\n"
        "JUSTIFICACION: El informe adjunto (informe.pdf, 2024-04-10) demuestra el "
        "cumplimiento de la obligación de reportar avances mensuales."
    )
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual"}],
        "matched_evidence": {
            "ob1": [{"source": "drive", "title": "informe.pdf", "link": "https://drive/x", "date": "2024-04-10"}]
        },
    }

    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)

    just = result["justificaciones"][0]
    assert just["actividad"] == "Elaboré y entregué el informe mensual de avance al supervisor."
    assert just["justificacion"].startswith("El informe adjunto")
    assert just["actividad"] != just["justificacion"]
    # Neither field echoes the obligación's own text.
    assert just["actividad"] != "Entregar informe mensual"
    assert just["justificacion"] != "Entregar informe mensual"
    assert just["origen"] == "llm"


@pytest.mark.asyncio
async def test_evidence_justify_near_identical_llm_output_falls_back_deterministically():
    """If the LLM (despite the FORBID rules) returns the SAME text for both ACTIVIDAD
    and JUSTIFICACION, the node must not persist two copies — justificacion falls
    back to the deterministic text instead."""
    from app.agent.nodes import evidence_justify as mod

    texto_repetido = "Se cumplió la obligación conforme a lo solicitado en el período."
    fake_resp = MagicMock()
    fake_resp.content = f"ACTIVIDAD: {texto_repetido}\nJUSTIFICACION: {texto_repetido}"
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual"}],
        "matched_evidence": {
            "ob1": [{"source": "drive", "title": "informe.pdf", "link": "https://drive/x", "date": "2024-04-10"}]
        },
    }

    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)

    just = result["justificaciones"][0]
    assert just["actividad"] == texto_repetido
    assert just["justificacion"] != texto_repetido
    assert just["actividad"] != just["justificacion"]
    assert just["origen"] == "seed"


# ─────────────────────────────────────────────────────────────────────────────
# Parallelization (radicacion-sin-friccion Phase 2 slice 2.6): every obligación's
# justificación generation must run CONCURRENTLY, not one LLM call at a time.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_justify_runs_llm_calls_concurrently_not_sequentially():
    """5 obligaciones, each with evidencia (so each triggers a real LLM call),
    each mocked to take 0.15s: sequential would take >= 0.75s; concurrent
    (asyncio.gather, no bound here) should land close to one call's delay."""
    import asyncio
    import time as time_module

    from app.agent.nodes import evidence_justify as mod

    async def _slow_complete(*args, **kwargs):
        await asyncio.sleep(0.15)
        resp = MagicMock()
        resp.content = "ACTIVIDAD: Actividad genérica.\nJUSTIFICACION: Justificación genérica distinta."
        return resp

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=_slow_complete)

    state = {
        "obligaciones_contexto": [{"id": f"ob{i}", "descripcion": f"Obligación número {i}"} for i in range(5)],
        "matched_evidence": {
            f"ob{i}": [{"source": "drive", "title": f"doc{i}.pdf", "link": f"https://drive/{i}", "date": "2024-04-10"}]
            for i in range(5)
        },
    }

    start = time_module.perf_counter()
    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)
    elapsed = time_module.perf_counter() - start

    assert mock_llm.complete.await_count == 5
    assert elapsed < 0.5, f"justify calls took {elapsed:.3f}s — looks sequential, not concurrent"
    assert len(result["justificaciones"]) == 5


@pytest.mark.asyncio
async def test_evidence_justify_result_order_matches_obligaciones_regardless_of_completion_order():
    """Correctness under concurrency: obligaciones whose LLM calls complete
    OUT OF ORDER (the first-listed obligación's call is the SLOWEST here, so
    it finishes LAST) must still land in the same position/content mapping as
    the sequential loop would have produced — `justificaciones[i]` always
    corresponds to `obligaciones[i]`, never to whichever call finished first."""
    import asyncio

    from app.agent.nodes import evidence_justify as mod

    # ob0's call is slowest (finishes last); ob2's is fastest (finishes first).
    delays = {"ob0": 0.12, "ob1": 0.06, "ob2": 0.01}
    obligacion_text = {
        "ob0": "Primera obligación del contrato",
        "ob1": "Segunda obligación del contrato",
        "ob2": "Tercera obligación del contrato",
    }

    async def _complete_with_variable_delay(messages, **kwargs):
        # The obligación's own text is embedded in the prompt (see
        # build_actividad_justificacion_prompt) — recover which obligación
        # this call is for by matching prompt content back to delays.
        user_msg = next(m for m in messages if m.role == "user")
        ob_id = next(k for k, texto in obligacion_text.items() if texto in user_msg.content)
        await asyncio.sleep(delays[ob_id])
        resp = MagicMock()
        resp.content = (
            f"ACTIVIDAD: Actividad para {ob_id}.\nJUSTIFICACION: Justificación específica para {ob_id} distinta."
        )
        return resp

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=_complete_with_variable_delay)

    state = {
        "obligaciones_contexto": [{"id": ob_id, "descripcion": texto} for ob_id, texto in obligacion_text.items()],
        "matched_evidence": {
            ob_id: [
                {"source": "drive", "title": f"{ob_id}.pdf", "link": f"https://drive/{ob_id}", "date": "2024-04-10"}
            ]
            for ob_id in obligacion_text
        },
    }

    with patch.object(mod, "get_llm", return_value=mock_llm):
        result = await mod.evidence_justify_node(state)

    justificaciones = result["justificaciones"]
    assert [j["obligacion_id"] for j in justificaciones] == ["ob0", "ob1", "ob2"]
    for j in justificaciones:
        assert j["obligacion_id"] in j["actividad"]
        assert j["obligacion_id"] in j["justificacion"]
