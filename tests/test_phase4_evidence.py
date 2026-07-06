"""Tests for Phase 4: evidence_orchestrator, local_files, evidence_matcher, evidence_dedup."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# evidence_orchestrator_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_orchestrator_merges_sources():
    """Merges email and local evidence into evidence_raw."""
    from app.agent.nodes.evidence_orchestrator import evidence_orchestrator_node

    state = {
        "email_evidencias": [
            {"subject": "Reunión semanal", "snippet": "Asistí a la reunión del lunes", "date": "2024-04-01", "message_id": "msg1"},
        ],
        "local_evidence": [
            {"filename": "informe.pdf", "text": "Informe de actividades abril 2024", "file_id": uuid.uuid4()},
        ],
    }
    result = await evidence_orchestrator_node(state)

    assert len(result["evidence_raw"]) == 2
    assert result["evidence_raw"][0]["source"] == "email"
    assert result["evidence_raw"][1]["source"] == "local_file"
    assert result["current_phase"] == "evidence_orchestrator"


@pytest.mark.asyncio
async def test_evidence_orchestrator_empty_sources():
    """Returns empty list when no evidence in state."""
    from app.agent.nodes.evidence_orchestrator import evidence_orchestrator_node

    result = await evidence_orchestrator_node({})

    assert result["evidence_raw"] == []
    assert result["current_phase"] == "evidence_orchestrator"


@pytest.mark.asyncio
async def test_evidence_orchestrator_email_only():
    """Works when only email evidence is present."""
    from app.agent.nodes.evidence_orchestrator import evidence_orchestrator_node

    state = {
        "email_evidencias": [
            {"subject": "Entrega informe", "snippet": "Adjunto informe", "date": "2024-04-05", "message_id": "msg2"},
            {"subject": "Reunión", "snippet": "Acta de reunión", "date": "2024-04-10", "message_id": "msg3"},
        ],
    }
    result = await evidence_orchestrator_node(state)

    assert len(result["evidence_raw"]) == 2
    for ev in result["evidence_raw"]:
        assert ev["source"] == "email"


# ─────────────────────────────────────────────────────────────────────────────
# local_files_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_files_no_file_ids():
    """Returns empty local_evidence when no uploaded_file_ids."""
    from app.agent.nodes.local_files import local_files_node

    result = await local_files_node({})

    assert result["local_evidence"] == []
    assert result["current_phase"] == "local_files"


@pytest.mark.asyncio
async def test_local_files_s3_failure_graceful():
    """Handles S3 download failure gracefully, skips file."""
    from app.agent.nodes.local_files import local_files_node

    file_id = uuid.uuid4()

    with patch("app.agent.nodes.local_files._load_file_from_s3", return_value=None):
        result = await local_files_node({"uploaded_file_ids": [file_id]})

    assert result["local_evidence"] == []
    assert result["current_phase"] == "local_files"


@pytest.mark.asyncio
async def test_local_files_text_extraction():
    """Extracts text from a text file correctly."""
    from app.agent.nodes.local_files import local_files_node

    file_id = uuid.uuid4()
    mock_data = b"Informe de actividades del mes de abril."

    async def mock_load(fid):
        return (mock_data, "informe.txt")

    with patch("app.agent.nodes.local_files._load_file_from_s3", side_effect=mock_load):
        result = await local_files_node({"uploaded_file_ids": [file_id]})

    assert len(result["local_evidence"]) == 1
    assert "Informe de actividades" in result["local_evidence"][0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# evidence_matcher_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_matcher_no_data():
    """Returns empty matched_evidence when no input."""
    from app.agent.nodes.evidence_matcher import evidence_matcher_node

    result = await evidence_matcher_node({})

    assert result["matched_evidence"] == {}
    assert result["current_phase"] == "evidence_matcher"


@pytest.mark.asyncio
async def test_evidence_matcher_keyword_scoring():
    """Keyword score function correctly identifies overlap."""
    from app.agent.nodes.evidence_matcher import _keyword_score

    ob = "Entregar informe mensual de actividades"
    ev = "Adjunto el informe mensual de actividades de abril"
    score = _keyword_score(ob, ev)

    assert score > 0.5  # High overlap


@pytest.mark.asyncio
async def test_evidence_matcher_low_overlap():
    """Returns low keyword score for unrelated text."""
    from app.agent.nodes.evidence_matcher import _keyword_score

    ob = "Entregar informe mensual de actividades"
    ev = "Buenos días, ¿cómo está usted?"
    score = _keyword_score(ob, ev)

    assert score < 0.2


@pytest.mark.asyncio
async def test_evidence_matcher_matches_relevant():
    """Matches evidence to obligation when LLM says RELEVANTE."""
    from app.agent.nodes.evidence_matcher import evidence_matcher_node

    fake_resp = MagicMock()
    fake_resp.content = "RELEVANTE"
    fake_resp.total_tokens = 10

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades contrato"}]
    evidence_raw = [
        {"source": "email", "content": "Adjunto informe mensual de actividades contrato abril", "subject": "Informe"},
    ]

    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=mock_llm):
        result = await evidence_matcher_node({
            "obligaciones_extraidas": obligaciones,
            "evidence_raw": evidence_raw,
        })

    assert "ob1" in result["matched_evidence"]
    assert len(result["matched_evidence"]["ob1"]) >= 1


@pytest.mark.asyncio
async def test_evidence_matcher_rejects_irrelevant():
    """Excludes evidence when LLM says NO_RELEVANTE."""
    from app.agent.nodes.evidence_matcher import evidence_matcher_node

    fake_resp = MagicMock()
    fake_resp.content = "NO_RELEVANTE"
    fake_resp.total_tokens = 10

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}]
    evidence_raw = [
        {"source": "email", "content": "Informe mensual actividades", "subject": "Informe"},
    ]

    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=mock_llm):
        result = await evidence_matcher_node({
            "obligaciones_extraidas": obligaciones,
            "evidence_raw": evidence_raw,
        })

    assert result["matched_evidence"].get("ob1", []) == []


# ─────────────────────────────────────────────────────────────────────────────
# evidence_dedup_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_dedup_removes_duplicates():
    """5 identical emails → 1 after dedup."""
    from app.agent.nodes.evidence_dedup import evidence_dedup_node

    duplicate_ev = {"source": "email", "content": "Asistí a la reunión del lunes 8 de abril"}
    state = {
        "evidence_raw": [duplicate_ev] * 5,
        "matched_evidence": {},
    }
    result = await evidence_dedup_node(state)

    assert len(result["deduplicated_evidence"]) == 1
    assert result["current_phase"] == "evidence_dedup"


@pytest.mark.asyncio
async def test_evidence_dedup_preserves_unique():
    """Unique evidence items are all preserved."""
    from app.agent.nodes.evidence_dedup import evidence_dedup_node

    state = {
        "evidence_raw": [
            {"source": "email", "content": "Reunión del lunes con el supervisor"},
            {"source": "email", "content": "Entrega del informe de actividades"},
            {"source": "local_file", "content": "Acta de visita de campo 15 de abril"},
        ],
        "matched_evidence": {},
    }
    result = await evidence_dedup_node(state)

    assert len(result["deduplicated_evidence"]) == 3


@pytest.mark.asyncio
async def test_evidence_dedup_empty_input():
    """Handles empty state gracefully."""
    from app.agent.nodes.evidence_dedup import evidence_dedup_node

    result = await evidence_dedup_node({})

    assert result["deduplicated_evidence"] == []
    assert result["current_phase"] == "evidence_dedup"


@pytest.mark.asyncio
async def test_evidence_dedup_hash_consistency():
    """Content hash is deterministic for same content."""
    from app.agent.nodes.evidence_dedup import _content_hash

    ev = {"content": "This is a test content for hashing"}
    h1 = _content_hash(ev)
    h2 = _content_hash(ev)

    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex length


@pytest.mark.asyncio
async def test_evidence_dedup_5_duplicates_to_1_group():
    """5 items with identical content must be deduped to exactly 1 unique item."""
    from app.agent.nodes.evidence_dedup import evidence_dedup_node

    duplicate_content = "Informe de actividades del mes de abril 2024"
    duplicates = [
        {"content": duplicate_content, "source": "email", "date": f"2024-04-0{i}"}
        for i in range(1, 6)
    ]
    state = {"evidence_raw": duplicates}
    result = await evidence_dedup_node(state)

    # Node writes deduplicated items to deduplicated_evidence
    deduped = result.get("deduplicated_evidence") or []
    assert len(deduped) == 1, f"Expected 1 unique item, got {len(deduped)}"
    assert result["current_phase"] == "evidence_dedup"


@pytest.mark.asyncio
async def test_evidence_nodes_wired_in_graph():
    """Phase 4 nodes are wired in the compiled graph."""
    from app.agent.graph import build_graph

    g = build_graph()
    for node in ["evidence_orchestrator", "evidence_matcher", "evidence_dedup", "local_files"]:
        assert node in g.nodes, f"Node {node} missing from graph"
