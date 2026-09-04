"""Tests for F3 — a mid-loop LLM failure must not 500 and must not lose history.

Root bug: `await llm.complete(...)` inside `chat_with_tools`'s loop sat OUTSIDE
any try/except. `LiteLLMAdapter.complete` raises `RuntimeError` when the whole
model fallback chain fails; that propagated out of `chat_with_tools`, so the
final `db.commit()` persisting the user message / assistant reply / recap never
ran — while any `write`-tagged tool that already ran in an EARLIER iteration had
already committed (cuenta created, credits spent, etc). The user's next message
then replayed a conversation with no record any of that happened.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _FailingAfterNScriptedLLM:
    """Returns scripted responses, then raises RuntimeError on the Nth call
    (mirrors `LiteLLMAdapter.complete`'s "all models failed" failure mode)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise RuntimeError("All LLM models failed. Last error: simulated total outage")
        return self._responses.pop(0)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: _FailingAfterNScriptedLLM) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"llmfail_{suffix}@example.com",
        nombre=f"LLM Failure User {suffix}",
        cedula=f"7171{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"LLMFAIL-{suffix}",
        objeto="Objeto de prueba para fallas de LLM",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"7171{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_llm_failure_after_successful_tool_call_does_not_raise(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, "01")

    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 7, "anio": 2026}
    )
    # First call succeeds with a tool call; second call (asking for the final
    # answer) raises — simulates the whole LLM fallback chain going down mid-loop.
    scripted = _FailingAfterNScriptedLLM([LLMResponse(content="", model="fake", tool_calls=[tool_call])])
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de julio", None, {})

    assert scripted.call_count == 2
    assert len(result.tool_events) == 1
    assert result.tool_events[0].status == "ok"
    # A clear Spanish message telling the user the model was unreachable but the
    # work already done was saved — never a raw exception message.
    assert "no pude" in result.content.lower() or "modelo" in result.content.lower()

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    contenidos = [str(m.get("content", "")) for m in convo.mensajes_json]
    assert "Crea mi cuenta de julio" in contenidos
    assert any("crear_cuenta_cobro" in c for c in contenidos), "the recap of the succeeded tool call must survive"


@pytest.mark.asyncio
async def test_llm_failure_on_first_call_still_persists_user_message(
    db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with ZERO successful tool calls, a total LLM outage must not 500 and
    must not silently drop the user's message from history."""
    user = test_user["user"]
    scripted = _FailingAfterNScriptedLLM([])
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Hola, ¿me ayudás?", None, {})

    assert result.tool_events == []
    assert result.content

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    contenidos = [str(m.get("content", "")) for m in convo.mensajes_json]
    assert "Hola, ¿me ayudás?" in contenidos


@pytest.mark.asyncio
async def test_llm_failure_returns_http_200_not_500(client: AsyncClient, test_user: dict[str, Any]) -> None:
    scripted = _FailingAfterNScriptedLLM([])

    with patch("app.services.agent_chat_service.get_llm", return_value=scripted):
        response = await client.post(
            "/api/v1/agent/chat",
            headers=test_user["headers"],
            data={"message": "Hola"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["content"]
