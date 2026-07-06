"""Tests for agent session SSE stream and HIL feedback endpoints (Phase 6)."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversacion import Conversacion
from app.models.agent_run import AgentRun

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def conversacion(db: AsyncSession, test_user: dict[str, Any]) -> Conversacion:
    conv = Conversacion(
        usuario_id=test_user["user"].id,
        mensajes_json=[],
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@pytest.fixture
async def agent_run(
    db: AsyncSession, test_user: dict[str, Any], conversacion: Conversacion
) -> AgentRun:
    run = AgentRun(
        usuario_id=test_user["user"].id,
        conversacion_id=conversacion.id,
        modo="cuenta_cobro",
        estado="pausado_hil",
        nodo_actual="human_review",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


# ---------------------------------------------------------------------------
# Stream endpoint tests
# ---------------------------------------------------------------------------


async def test_stream_requires_auth(client: AsyncClient) -> None:
    fake = str(uuid.uuid4())
    response = await client.get(f"/api/v1/agent/sessions/{fake}/stream")
    assert response.status_code == 401


async def test_stream_404_for_unknown_session(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    fake = str(uuid.uuid4())
    response = await client.get(
        f"/api/v1/agent/sessions/{fake}/stream",
        headers=test_user["headers"],
    )
    assert response.status_code == 404


async def test_stream_returns_event_stream(
    client: AsyncClient,
    test_user: dict[str, Any],
    conversacion: Conversacion,
) -> None:
    response = await client.get(
        f"/api/v1/agent/sessions/{conversacion.id}/stream",
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    ct = response.headers.get("content-type", "")
    assert "text/event-stream" in ct


# ---------------------------------------------------------------------------
# Feedback endpoint tests
# ---------------------------------------------------------------------------


async def test_feedback_requires_auth(client: AsyncClient) -> None:
    fake = str(uuid.uuid4())
    response = await client.patch(
        f"/api/v1/agent/sessions/{fake}/feedback",
        json={"feedback": "ok", "action": "continue"},
    )
    assert response.status_code == 401


async def test_feedback_404_for_unknown_session(
    client: AsyncClient, test_user: dict[str, Any]
) -> None:
    fake = str(uuid.uuid4())
    response = await client.patch(
        f"/api/v1/agent/sessions/{fake}/feedback",
        headers=test_user["headers"],
        json={"feedback": "ok", "action": "continue"},
    )
    assert response.status_code == 404


async def test_feedback_abort_action(
    client: AsyncClient,
    test_user: dict[str, Any],
    agent_run: AgentRun,
) -> None:
    session_id = str(agent_run.conversacion_id)
    response = await client.patch(
        f"/api/v1/agent/sessions/{session_id}/feedback",
        headers=test_user["headers"],
        json={"feedback": "No sirve", "action": "abort"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "aborted"


async def test_feedback_continue_action(
    client: AsyncClient,
    test_user: dict[str, Any],
    agent_run: AgentRun,
) -> None:
    session_id = str(agent_run.conversacion_id)
    response = await client.patch(
        f"/api/v1/agent/sessions/{session_id}/feedback",
        headers=test_user["headers"],
        json={"feedback": "Aprobado", "action": "continue"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resumed"


# ---------------------------------------------------------------------------
# Run history endpoint tests
# ---------------------------------------------------------------------------


async def test_list_runs_requires_auth(client: AsyncClient) -> None:
    fake = str(uuid.uuid4())
    response = await client.get(f"/api/v1/agent/sessions/{fake}/runs")
    assert response.status_code == 401


async def test_list_runs_returns_list(
    client: AsyncClient,
    test_user: dict[str, Any],
    agent_run: AgentRun,
) -> None:
    session_id = str(agent_run.conversacion_id)
    response = await client.get(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1

