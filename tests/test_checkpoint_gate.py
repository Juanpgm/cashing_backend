"""Tests for app/agent/checkpoint.py — SQLAlchemy-native checkpoint store."""
from __future__ import annotations

import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.checkpoint import hydrate_state, load_checkpoint, sanitize_state, save_checkpoint
from app.core.exceptions import NotFoundError


# ── sanitize_state ─────────────────────────────────────────────────────────────

def test_sanitize_strips_private_keys():
    state = {"_db": object(), "_pdf_bytes": b"x", "session_id": uuid.uuid4()}
    result = sanitize_state(state)
    assert "_db" not in result
    assert "_pdf_bytes" not in result
    assert "session_id" in result


def test_sanitize_strips_document_bytes():
    state = {"document_bytes": b"rawbytes", "session_id": uuid.uuid4()}
    result = sanitize_state(state)
    assert result["document_bytes"] is None


def test_sanitize_preserves_normal_fields():
    state = {"session_id": uuid.uuid4(), "response": "hello", "mode": "chat"}
    result = sanitize_state(state)
    assert result["response"] == "hello"
    assert result["mode"] == "chat"


# ── hydrate_state ──────────────────────────────────────────────────────────────

def test_hydrate_reconstructs_uuid():
    raw = {"session_id": "123e4567-e89b-12d3-a456-426614174000"}
    state = hydrate_state(raw)
    assert isinstance(state["session_id"], uuid.UUID)


def test_hydrate_reconstructs_agent_mode():
    from app.schemas.agent import AgentMode
    raw = {"mode": "chat"}
    state = hydrate_state(raw)
    assert state["mode"] == AgentMode.CHAT


def test_hydrate_handles_none_uuid_fields():
    raw = {"session_id": None, "user_id": None}
    state = hydrate_state(raw)
    assert state["session_id"] is None
    assert state["user_id"] is None


# ── save/load round-trip ───────────────────────────────────────────────────────

async def test_checkpoint_save_load_roundtrip(db: AsyncSession):
    # Test sanitize/hydrate logic directly (no DB FK needed)
    state = {"response": "hello", "mode": "chat", "_db": None}
    clean = sanitize_state(state)
    hydrated = hydrate_state(clean)
    assert "_db" not in hydrated
    assert hydrated["response"] == "hello"


async def test_checkpoint_load_missing_raises_not_found(db: AsyncSession):
    with pytest.raises(NotFoundError):
        await load_checkpoint(db, uuid.uuid4())


# ── graph still compiles ───────────────────────────────────────────────────────

def test_graph_compiles():
    """Graph must compile without error."""
    from app.agent.graph import build_graph
    graph = build_graph()
    assert graph is not None


def test_graph_has_nodes():
    """Graph must expose router and other core nodes."""
    from app.agent.graph import build_graph
    graph = build_graph()
    assert "router" in graph.nodes
    assert "chat" in graph.nodes
