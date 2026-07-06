"""Tests for Phase 6: supervisor_node, human_review_node, CUENTA_COBRO_FULL routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# supervisor_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_routes_to_obligations_when_none():
    """Routes to obligations_extraction when no obligations in state."""
    from app.agent.nodes.supervisor import supervisor_node

    fake_resp = MagicMock()
    fake_resp.content = "obligations_extraction"
    fake_resp.total_tokens = 10

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    with patch("app.agent.nodes.supervisor.get_llm", return_value=mock_llm):
        result = await supervisor_node({})

    assert result["supervisor_plan"][0] == "obligations_extraction"
    assert result["current_phase"] == "supervisor"


@pytest.mark.asyncio
async def test_supervisor_routes_to_quality_gate_after_obligations():
    """Routes to quality_gate when obligations exist but quality not checked."""
    from app.agent.nodes.supervisor import supervisor_node

    fake_resp = MagicMock()
    fake_resp.content = "quality_gate"
    fake_resp.total_tokens = 10

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    state = {"obligaciones_extraidas": [{"descripcion": "Entregar informe"}]}

    with patch("app.agent.nodes.supervisor.get_llm", return_value=mock_llm):
        result = await supervisor_node(state)

    assert result["supervisor_plan"][0] == "quality_gate"


@pytest.mark.asyncio
async def test_supervisor_deterministic_fallback():
    """Uses deterministic fallback when LLM fails."""
    from app.agent.nodes.supervisor import _determine_next_node, supervisor_node

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("timeout"))

    # State: has obligations + quality passed + no evidence
    state = {
        "obligaciones_extraidas": [{"descripcion": "test"}],
        "quality_gate_passed": True,
    }

    with patch("app.agent.nodes.supervisor.get_llm", return_value=mock_llm):
        result = await supervisor_node(state)

    # Should fall back to evidence_orchestrator
    assert result["supervisor_plan"][0] == "evidence_orchestrator"
    assert result["current_phase"] == "supervisor"


@pytest.mark.asyncio
async def test_supervisor_determine_next_node_full_pipeline():
    """_determine_next_node selects correctly at each pipeline stage."""
    from app.agent.nodes.supervisor import _determine_next_node

    # Stage 1: no obligations
    assert _determine_next_node({}) == "obligations_extraction"

    # Stage 2: obligations but no quality check
    assert _determine_next_node({"obligaciones_extraidas": [{}]}) == "quality_gate"

    # Stage 3: quality passed, no evidence
    assert _determine_next_node({
        "obligaciones_extraidas": [{}],
        "quality_gate_passed": True,
    }) == "evidence_orchestrator"

    # Stage 4: has evidence, no dedup
    assert _determine_next_node({
        "obligaciones_extraidas": [{}],
        "quality_gate_passed": True,
        "evidence_raw": [{}],
    }) == "evidence_dedup"

    # Stage 5: dedup done, no drafts
    assert _determine_next_node({
        "obligaciones_extraidas": [{}],
        "quality_gate_passed": True,
        "evidence_raw": [{}],
        "deduplicated_evidence": [{}],
    }) == "doc_assembly"

    # Stage 6: drafts done, no manifest
    assert _determine_next_node({
        "obligaciones_extraidas": [{}],
        "quality_gate_passed": True,
        "evidence_raw": [{}],
        "deduplicated_evidence": [{}],
        "document_drafts": [{}],
    }) == "folder_organizer"

    # Stage 7: manifest done, no preview
    assert _determine_next_node({
        "obligaciones_extraidas": [{}],
        "quality_gate_passed": True,
        "evidence_raw": [{}],
        "deduplicated_evidence": [{}],
        "document_drafts": [{}],
        "folder_manifest": {"cuenta_cobro": "/path"},
    }) == "human_review"

    # Stage 8: all done
    assert _determine_next_node({
        "obligaciones_extraidas": [{}],
        "quality_gate_passed": True,
        "evidence_raw": [{}],
        "deduplicated_evidence": [{}],
        "document_drafts": [{}],
        "folder_manifest": {"cuenta_cobro": "/path"},
        "preview_approved": True,
    }) == "END"


@pytest.mark.asyncio
async def test_supervisor_ignores_invalid_llm_response():
    """Falls back to deterministic when LLM returns invalid node name."""
    from app.agent.nodes.supervisor import supervisor_node

    fake_resp = MagicMock()
    fake_resp.content = "invalid_node_xyz"
    fake_resp.total_tokens = 5

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    with patch("app.agent.nodes.supervisor.get_llm", return_value=mock_llm):
        result = await supervisor_node({})

    # Deterministic fallback: no obligations → obligations_extraction
    assert result["supervisor_plan"][0] == "obligations_extraction"


# ─────────────────────────────────────────────────────────────────────────────
# human_review_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_human_review_interrupts():
    """human_review_node raises HumanInterrupt when hil_feedback is None."""
    from app.agent.engine import HumanInterrupt
    from app.agent.nodes.human_review import human_review_node

    state = {
        "document_drafts": [{"type": "cuenta_cobro", "content": "Draft content here"}],
        "hil_feedback": None,
    }

    with pytest.raises(HumanInterrupt):
        await human_review_node(state)


@pytest.mark.asyncio
async def test_human_review_build_message_quality_issues():
    """Builds quality-issue message when quality gate failed."""
    from app.agent.nodes.human_review import _build_review_message

    state = {
        "quality_gate_passed": False,
        "quality_issues": ["Faltan plazos en 3 obligaciones", "Sin referencia de cláusula"],
    }
    message = _build_review_message(state)

    assert "Faltan plazos" in message
    assert "continuar" in message.lower() or "proceder" in message.lower()


@pytest.mark.asyncio
async def test_human_review_build_message_final_review():
    """Builds final review message when drafts are ready."""
    from app.agent.nodes.human_review import _build_review_message

    state = {
        "document_drafts": [{"type": "cuenta_cobro", "content": "CUENTA DE COBRO"}],
    }
    message = _build_review_message(state)

    assert "PDF" in message or "borrador" in message.lower() or "aprobar" in message.lower()


@pytest.mark.asyncio
async def test_human_review_build_message_hil_reason():
    """Uses hil_reason as message when set."""
    from app.agent.nodes.human_review import _build_review_message

    state = {"hil_reason": "Necesita plantilla personalizada para esta entidad."}
    message = _build_review_message(state)

    assert message == "Necesita plantilla personalizada para esta entidad."


# ─────────────────────────────────────────────────────────────────────────────
# Graph integration tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase6_nodes_wired_in_graph():
    """Phase 6 nodes are wired in the compiled graph."""
    from app.agent.graph import build_graph

    g = build_graph()
    assert "supervisor" in g.nodes
    assert "human_review" in g.nodes


@pytest.mark.asyncio
async def test_cuenta_cobro_full_mode_routes_to_supervisor():
    """CUENTA_COBRO_FULL mode routes to supervisor node."""
    from app.agent.graph import _route_by_mode
    from app.schemas.agent import AgentMode

    state = {"mode": AgentMode.CUENTA_COBRO_FULL}
    destination = _route_by_mode(state)

    assert destination == "supervisor"


@pytest.mark.asyncio
async def test_route_from_supervisor_valid_nodes():
    """_route_from_supervisor correctly routes to each valid node."""
    from app.agent.graph import _route_from_supervisor

    for node in ["obligations_extraction", "quality_gate", "evidence_orchestrator",
                 "evidence_dedup", "doc_assembly", "folder_organizer", "human_review"]:
        state = {"supervisor_plan": [node]}
        result = _route_from_supervisor(state)
        assert result == node, f"Expected {node}, got {result}"


@pytest.mark.asyncio
async def test_route_from_supervisor_empty_plan():
    """Empty supervisor_plan routes to human_review."""
    from app.agent.graph import _route_from_supervisor

    result = _route_from_supervisor({"supervisor_plan": []})
    assert result == "human_review"


@pytest.mark.asyncio
async def test_route_from_supervisor_invalid_node_falls_back():
    """Invalid node name in plan falls back to human_review."""
    from app.agent.graph import _route_from_supervisor

    result = _route_from_supervisor({"supervisor_plan": ["nonexistent_node"]})
    assert result == "human_review"


# ─────────────────────────────────────────────────────────────────────────────
# Borradores versioning — 3 iterations preserved
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_borradores_3_versions_all_preserved(db, test_user):
    """Creating 3 BorradorCuentaCobro versions preserves all 3 and v3 is latest."""
    import uuid as _uuid
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
    from app.models.contrato import Contrato
    from app.models.cuenta_cobro import CuentaCobro

    # Create a minimal contrato + cuenta_cobro fixture
    contrato = Contrato(
        id=_uuid.uuid4(),
        usuario_id=test_user["user"].id,
        entidad="SENA Regional Bogotá",
        numero_contrato="SENA-001-2024",
        objeto="Prestación de servicios de formación",
        valor_total=10_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=datetime(2024, 1, 1, tzinfo=timezone.utc).date(),
        fecha_fin=datetime(2024, 12, 31, tzinfo=timezone.utc).date(),
    )
    db.add(contrato)
    await db.flush()

    cuenta = CuentaCobro(
        id=_uuid.uuid4(),
        contrato_id=contrato.id,
        mes=4,
        anio=2024,
    )
    db.add(cuenta)
    await db.flush()

    # Create 3 borrador versions
    for v in range(1, 4):
        borrador = BorradorCuentaCobro(
            id=_uuid.uuid4(),
            cuenta_cobro_id=cuenta.id,
            version=v,
            contenido={"html": f"<p>Version {v}</p>", "version": v},
            aprobado=(v == 3),  # only latest is approved in this test
        )
        db.add(borrador)
    await db.commit()

    # Verify all 3 versions are persisted
    result = await db.execute(
        select(BorradorCuentaCobro)
        .where(BorradorCuentaCobro.cuenta_cobro_id == cuenta.id)
        .order_by(BorradorCuentaCobro.version)
    )
    borradores = result.scalars().all()

    assert len(borradores) == 3, f"Expected 3 borradores, got {len(borradores)}"
    assert borradores[0].version == 1
    assert borradores[1].version == 2
    assert borradores[2].version == 3
    # Latest (v3) is approved; earlier versions are not
    assert borradores[2].aprobado is True
    assert borradores[0].aprobado is not True

