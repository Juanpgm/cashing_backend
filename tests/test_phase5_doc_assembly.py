"""Tests for Phase 5: doc_assembly_node, folder_organizer_node."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# doc_assembly_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_doc_assembly_no_contrato():
    """Returns error when contrato_extraido is absent."""
    from app.agent.nodes.doc_assembly import doc_assembly_node

    result = await doc_assembly_node({})

    assert "error" in result
    assert result["current_phase"] == "doc_assembly"


@pytest.mark.asyncio
async def test_doc_assembly_generates_cuenta_cobro():
    """Generates cuenta_cobro draft with LLM."""
    from app.agent.nodes.doc_assembly import doc_assembly_node

    fake_resp = MagicMock()
    fake_resp.content = "CUENTA DE COBRO No. 001\nEntidad: DIAN\nValor: $5,000,000"
    fake_resp.total_tokens = 300

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {
        "document_type": "cuenta_cobro",
        "contrato_extraido": {
            "entidad": "DIAN",
            "numero_contrato": "001-2024",
            "objeto": "Servicios de consultoría",
            "valor_mensual": "5000000",
            "contratista": "Juan Pérez",
        },
        "obligaciones_extraidas": [{"descripcion": "Entregar informe mensual"}],
        "deduplicated_evidence": [{"source": "email", "content": "Informe adjunto", "subject": "Informe"}],
        "mes": 4,
        "anio": 2024,
    }

    with patch("app.agent.nodes.doc_assembly.get_llm", return_value=mock_llm):
        result = await doc_assembly_node(state)

    assert result["document_drafts"] is not None
    assert len(result["document_drafts"]) == 1
    assert result["document_drafts"][0]["type"] == "cuenta_cobro"
    assert "CUENTA DE COBRO" in result["document_drafts"][0]["content"]
    assert result["preview_html"] is not None
    assert result["current_phase"] == "doc_assembly"


@pytest.mark.asyncio
async def test_doc_assembly_generates_informe():
    """Generates informe_actividades draft."""
    from app.agent.nodes.doc_assembly import doc_assembly_node

    fake_resp = MagicMock()
    fake_resp.content = "INFORME DE ACTIVIDADES\nPeríodo: Abril 2024"
    fake_resp.total_tokens = 250

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {
        "document_type": "informe_actividades",
        "contrato_extraido": {"entidad": "Alcaldía", "numero_contrato": "002-2024"},
        "obligaciones_extraidas": [],
        "mes": 4,
        "anio": 2024,
    }

    with patch("app.agent.nodes.doc_assembly.get_llm", return_value=mock_llm):
        result = await doc_assembly_node(state)

    assert result["document_drafts"][0]["type"] == "informe_actividades"
    assert "INFORME" in result["document_drafts"][0]["content"]


@pytest.mark.asyncio
async def test_doc_assembly_llm_failure_produces_fallback():
    """When LLM fails, draft has error placeholder but doesn't crash."""
    from app.agent.nodes.doc_assembly import doc_assembly_node

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("model unavailable"))

    state = {
        "document_type": "cuenta_cobro",
        "contrato_extraido": {"entidad": "DIAN", "numero_contrato": "001-2024"},
        "mes": 4,
        "anio": 2024,
    }

    with patch("app.agent.nodes.doc_assembly.get_llm", return_value=mock_llm):
        result = await doc_assembly_node(state)

    assert result["document_drafts"] is not None
    assert "[Error" in result["document_drafts"][0]["content"]
    assert result["current_phase"] == "doc_assembly"


@pytest.mark.asyncio
async def test_doc_assembly_preview_html_contains_content():
    """Preview HTML wraps the draft content."""
    from app.agent.nodes.doc_assembly import doc_assembly_node

    fake_resp = MagicMock()
    fake_resp.content = "Mi cuenta de cobro"
    fake_resp.total_tokens = 50

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {
        "document_type": "cuenta_cobro",
        "contrato_extraido": {"entidad": "Test", "numero_contrato": "T-001"},
        "mes": 1,
        "anio": 2024,
    }

    with patch("app.agent.nodes.doc_assembly.get_llm", return_value=mock_llm):
        result = await doc_assembly_node(state)

    assert "Mi cuenta de cobro" in result["preview_html"]
    assert "<html>" in result["preview_html"]


# ─────────────────────────────────────────────────────────────────────────────
# folder_organizer_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_folder_organizer_builds_correct_structure():
    """Creates correct path structure: entidad/contrato/YYYY-MM/tipo/."""
    from app.agent.nodes.folder_organizer import folder_organizer_node

    state = {
        "document_drafts": [{"type": "cuenta_cobro", "content": "...", "mes": 4, "anio": 2024}],
        "contrato_extraido": {"entidad": "Alcaldía de Medellín", "numero_contrato": "001-2024"},
        "mes": 4,
        "anio": 2024,
    }
    result = await folder_organizer_node(state)

    assert "cuenta_cobro" in result["folder_manifest"]
    path = result["folder_manifest"]["cuenta_cobro"]
    assert "alcaldia-de-medellin" in path
    assert "001-2024" in path
    assert "2024-04" in path
    assert "cuenta-cobro" in path
    assert result["current_phase"] == "folder_organizer"


@pytest.mark.asyncio
async def test_folder_organizer_multiple_doc_types():
    """Creates separate paths for multiple document types."""
    from app.agent.nodes.folder_organizer import folder_organizer_node

    state = {
        "document_drafts": [
            {"type": "cuenta_cobro", "content": "..."},
            {"type": "informe_actividades", "content": "..."},
        ],
        "contrato_extraido": {"entidad": "DIAN", "numero_contrato": "DC-001"},
        "mes": 3,
        "anio": 2024,
    }
    result = await folder_organizer_node(state)

    assert "cuenta_cobro" in result["folder_manifest"]
    assert "informe_actividades" in result["folder_manifest"]
    # Paths should be different
    assert result["folder_manifest"]["cuenta_cobro"] != result["folder_manifest"]["informe_actividades"]


@pytest.mark.asyncio
async def test_folder_organizer_no_drafts():
    """Works without drafts — defaults to cuenta_cobro."""
    from app.agent.nodes.folder_organizer import folder_organizer_node

    state = {
        "contrato_extraido": {"entidad": "DIAN", "numero_contrato": "DC-001"},
        "mes": 3,
        "anio": 2024,
    }
    result = await folder_organizer_node(state)

    assert len(result["folder_manifest"]) >= 1
    assert result["current_phase"] == "folder_organizer"


@pytest.mark.asyncio
async def test_folder_organizer_slugify():
    """Slugify handles special Spanish characters."""
    from app.agent.nodes.folder_organizer import _slugify

    assert _slugify("Alcaldía de Bogotá") == "alcaldia-de-bogota"
    assert _slugify("GOBERNACIÓN ANTIOQUIA") == "gobernacion-antioquia"
    assert _slugify("Ñoño Corporation") == "nono-corporation"


@pytest.mark.asyncio
async def test_phase5_nodes_wired_in_graph():
    """Phase 5 nodes are wired in the compiled graph."""
    from app.agent.graph import build_graph

    g = build_graph()
    assert "doc_assembly" in g.nodes
    assert "folder_organizer" in g.nodes
