"""Tests for `POST /api/v1/agent/chat/stream` and its tool-call control endpoints
(approve/reject/cancel/retry) — radicacion-sin-friccion 3.10.

The full live approval/cancel/retry STATE MACHINE (through a real streamed turn) is
covered at the service layer in `test_agent_chat_stream_service.py`. This file
covers the HTTP surface: auth, request/response shape, SSE content-type, and —
critically — the control endpoints' ownership/no-op contract, exercised against
pending entries seeded directly via `app.services.agent_tool_approval` (no live
gate coroutine needed for these — see that module's docstring on why `resolve()`
alone is enough to make status transitions deterministic without one).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from app.core.security import create_access_token, hash_password
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse
from app.services import agent_tool_approval
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


class _ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        return self._responses.pop(0)


async def _make_second_user(db: AsyncSession) -> dict[str, Any]:
    user = Usuario(
        email="agent_stream_api_other@example.com",
        nombre="Other User",
        cedula="5050001",
        telefono="+573001234567",
        password_hash=hash_password("TestPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token(subject=str(user.id), role=user.rol)
    return {"user": user, "token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture(autouse=True)
def _clear_approval_store() -> Any:
    agent_tool_approval.clear()
    yield
    agent_tool_approval.clear()


# ---------------------------------------------------------------------------
# POST /chat/stream
# ---------------------------------------------------------------------------


async def test_stream_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/agent/chat/stream", data={"message": "Hola"})
    assert response.status_code == 401


async def test_stream_returns_sse_with_final_event(client: AsyncClient, test_user: dict[str, Any]) -> None:
    fake_llm = _ScriptedLLM([LLMResponse(content="Hola, ¿en qué te ayudo?", model="fake", total_tokens=5)])

    with patch("app.services.agent_chat_service.get_llm", return_value=fake_llm):
        response = await client.post(
            "/api/v1/agent/chat/stream",
            headers=test_user["headers"],
            data={"message": "Hola"},
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    body = response.text
    assert '"type": "connected"' in body or '"type":"connected"' in body
    assert "Hola, ¿en qué te ayudo?" in body
    assert '"type": "final"' in body or '"type":"final"' in body


# ---------------------------------------------------------------------------
# Tool-call control endpoints — auth / ownership / no-op contract
# ---------------------------------------------------------------------------


async def test_approve_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/agent/chat/stream/some-session/tool-calls/some-call/approve")
    assert response.status_code == 401


async def test_approve_unknown_call_id_is_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/agent/chat/stream/some-session/tool-calls/does-not-exist/approve",
        headers=test_user["headers"],
    )
    assert response.status_code == 404


async def test_approve_cross_user_is_404_and_owner_still_succeeds_after(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
) -> None:
    other = await _make_second_user(db)
    agent_tool_approval.register(
        "session-x", "call-x", test_user["user"].id, "crear_cuenta_cobro", {"contrato_id": "x"}
    )

    response = await client.post(
        "/api/v1/agent/chat/stream/session-x/tool-calls/call-x/approve",
        headers=other["headers"],
    )
    assert response.status_code == 404

    # The owner's own request is unaffected by the other user's failed attempt.
    response = await client.post(
        "/api/v1/agent/chat/stream/session-x/tool-calls/call-x/approve",
        headers=test_user["headers"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "approved"
    assert body["status"] == "approved"


async def test_reject_twice_second_call_is_noop(client: AsyncClient, test_user: dict[str, Any]) -> None:
    agent_tool_approval.register("session-y", "call-y", test_user["user"].id, "radicar_cuenta", {})

    first = await client.post(
        "/api/v1/agent/chat/stream/session-y/tool-calls/call-y/reject", headers=test_user["headers"]
    )
    assert first.status_code == 200
    assert first.json()["action"] == "rejected"

    second = await client.post(
        "/api/v1/agent/chat/stream/session-y/tool-calls/call-y/reject", headers=test_user["headers"]
    )
    assert second.status_code == 200
    assert second.json()["action"] == "no_op"


async def test_cancel_already_running_is_noop_not_a_crash(client: AsyncClient, test_user: dict[str, Any]) -> None:
    entry = agent_tool_approval.register("session-z", "call-z", test_user["user"].id, "editar_actividad", {})
    entry.status = "running"  # simulate the race: the handler is already in flight

    response = await client.post(
        "/api/v1/agent/chat/stream/session-z/tool-calls/call-z/cancel", headers=test_user["headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "no_op"
    assert "running" in body["message"]


async def test_cancel_already_succeeded_is_noop(client: AsyncClient, test_user: dict[str, Any]) -> None:
    entry = agent_tool_approval.register("session-w", "call-w", test_user["user"].id, "persistir_evidencias", {})
    entry.status = "success"

    response = await client.post(
        "/api/v1/agent/chat/stream/session-w/tool-calls/call-w/cancel", headers=test_user["headers"]
    )
    assert response.status_code == 200
    assert response.json()["action"] == "no_op"


async def test_retry_only_valid_while_awaiting_retry_decision(client: AsyncClient, test_user: dict[str, Any]) -> None:
    agent_tool_approval.register("session-v", "call-v", test_user["user"].id, "marcar_requisito", {})

    # Still awaiting approval (never failed yet) — retry makes no sense here.
    too_early = await client.post(
        "/api/v1/agent/chat/stream/session-v/tool-calls/call-v/retry", headers=test_user["headers"]
    )
    assert too_early.status_code == 200
    assert too_early.json()["action"] == "no_op"

    agent_tool_approval.reopen_for_retry_decision("session-v", "call-v")
    ok = await client.post("/api/v1/agent/chat/stream/session-v/tool-calls/call-v/retry", headers=test_user["headers"])
    assert ok.status_code == 200
    assert ok.json()["action"] == "retry_requested"
