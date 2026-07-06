"""Tests for Phase 2: requirements_ingestion_node, entity_profile_node, template_resolver_node."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# requirements_ingestion_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requirements_ingestion_test_mode():
    """Test-mode bypass when document_text starts with __."""
    from app.agent.nodes.requirements_ingestion import requirements_ingestion_node

    state = {"document_text": "__test_mode_bypass__"}
    result = await requirements_ingestion_node(state)

    assert result["entity_requirements"]["entidad"] == "test"
    assert result["current_phase"] == "requirements_ingestion"
    assert "error" not in result


@pytest.mark.asyncio
async def test_requirements_ingestion_no_text():
    """Returns error when no document_text provided."""
    from app.agent.nodes.requirements_ingestion import requirements_ingestion_node

    state = {}
    result = await requirements_ingestion_node(state)

    assert "error" in result
    assert result["current_phase"] == "requirements_ingestion"


@pytest.mark.asyncio
async def test_requirements_ingestion_llm_success():
    """Calls LLM and parses JSON result correctly."""
    from app.agent.nodes.requirements_ingestion import requirements_ingestion_node

    fake_resp = MagicMock()
    fake_resp.content = '{"entidad": "DIAN", "tipo_entidad": "publica", "campos_requeridos": ["RUT", "certificado"]}'
    fake_resp.total_tokens = 100

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    with patch("app.agent.nodes.requirements_ingestion.get_llm", return_value=mock_llm):
        state = {"document_text": "El proveedor debe presentar RUT y certificado a la DIAN."}
        result = await requirements_ingestion_node(state)

    assert result["entity_requirements"]["entidad"] == "DIAN"
    assert "RUT" in result["entity_requirements"]["campos_requeridos"]
    assert result["current_phase"] == "requirements_ingestion"


@pytest.mark.asyncio
async def test_requirements_ingestion_llm_error():
    """Returns error state when LLM fails."""
    from app.agent.nodes.requirements_ingestion import requirements_ingestion_node

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    with patch("app.agent.nodes.requirements_ingestion.get_llm", return_value=mock_llm):
        state = {"document_text": "Some document text."}
        result = await requirements_ingestion_node(state)

    assert "error" in result
    assert result["current_phase"] == "requirements_ingestion"


@pytest.mark.asyncio
async def test_requirements_ingestion_fallback_user_input():
    """Falls back to user_input when document_text is absent."""
    from app.agent.nodes.requirements_ingestion import requirements_ingestion_node

    fake_resp = MagicMock()
    fake_resp.content = '{"entidad": "Alcaldía", "campos_requeridos": []}'
    fake_resp.total_tokens = 50

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    with patch("app.agent.nodes.requirements_ingestion.get_llm", return_value=mock_llm):
        state = {"user_input": "Enviar cuenta a la Alcaldía."}
        result = await requirements_ingestion_node(state)

    assert result["entity_requirements"]["entidad"] == "Alcaldía"


# ─────────────────────────────────────────────────────────────────────────────
# entity_profile_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entity_profile_creates_deterministic_id():
    """Same entity name always produces the same profile UUID."""
    from app.agent.nodes.entity_profile import entity_profile_node

    state = {
        "entity_requirements": {"entidad": "Alcaldía de Medellín"},
        "contrato_extraido": {},
    }
    r1 = await entity_profile_node(state)
    r2 = await entity_profile_node(state)

    assert r1["entity_profile_id"] == r2["entity_profile_id"]
    assert r1["current_phase"] == "entity_profile"


@pytest.mark.asyncio
async def test_entity_profile_reuses_existing_id():
    """When entity_profile_id already in state, it is preserved."""
    from app.agent.nodes.entity_profile import entity_profile_node

    existing = uuid.uuid4()
    state = {
        "entity_requirements": {"entidad": "DIAN"},
        "contrato_extraido": {},
        "entity_profile_id": existing,
    }
    result = await entity_profile_node(state)

    assert result["entity_profile_id"] == existing


@pytest.mark.asyncio
async def test_entity_profile_falls_back_to_contrato():
    """Uses contrato_extraido.entidad when entity_requirements has none."""
    from app.agent.nodes.entity_profile import entity_profile_node

    state = {
        "entity_requirements": {},
        "contrato_extraido": {"entidad": "Gobernación de Antioquia"},
    }
    result = await entity_profile_node(state)

    assert result["entity_profile_id"] is not None
    assert result["current_phase"] == "entity_profile"


@pytest.mark.asyncio
async def test_entity_profile_no_entity_returns_error():
    """Returns error when no entity can be determined."""
    from app.agent.nodes.entity_profile import entity_profile_node

    state = {"entity_requirements": {}, "contrato_extraido": {}}
    result = await entity_profile_node(state)

    assert "error" in result
    assert result["current_phase"] == "entity_profile"


# ─────────────────────────────────────────────────────────────────────────────
# template_resolver_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_template_resolver_known_type_no_interrupt():
    """Returns template_id for known document_type without interrupting."""
    from app.agent.nodes.template_resolver import template_resolver_node

    state = {
        "document_type": "cuenta_cobro",
        "entity_profile_id": uuid.uuid4(),
    }
    result = await template_resolver_node(state)

    assert result["template_id"] is not None
    assert result["document_type"] == "cuenta_cobro"
    assert result["hil_reason"] is None
    assert result["current_phase"] == "template_resolver"


@pytest.mark.asyncio
async def test_template_resolver_informe_actividades():
    """Resolves informe_actividades type."""
    from app.agent.nodes.template_resolver import template_resolver_node

    state = {
        "document_type": "informe_actividades",
        "entity_profile_id": uuid.uuid4(),
    }
    result = await template_resolver_node(state)

    assert result["template_id"] is not None
    assert str(result["template_id"]) == "00000000-0000-4000-8000-000000000002"


@pytest.mark.asyncio
async def test_template_resolver_interrupts_on_missing_type():
    """Raises HumanInterrupt when document_type is None and no hil_feedback."""
    from app.agent.engine import HumanInterrupt
    from app.agent.nodes.template_resolver import template_resolver_node

    state = {"entity_profile_id": uuid.uuid4()}

    with pytest.raises(HumanInterrupt):
        await template_resolver_node(state)


@pytest.mark.asyncio
async def test_graph_has_template_resolver_node():
    """Confirm template_resolver is wired in the compiled graph."""
    from app.agent.graph import build_graph

    g = build_graph()
    assert "template_resolver" in g.nodes
