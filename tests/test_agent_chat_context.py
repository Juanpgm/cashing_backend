"""Tests for the optional "contract context" passed to the free-form agent chat.

Covers `agent_chat_service._resolve_contrato_context` (defensive resolution: never
raises, falls back to `None` on any bad/foreign/missing id) and the system-prompt
injection wired through `chat_with_tools`. See `tests/test_agent_chat_api.py` for the
endpoint-level `contrato_id` Form field contract.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse
from app.services import agent_chat_service
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_agent_chat_service import ScriptedLLM, _patch_llm


class CapturingScriptedLLM(ScriptedLLM):
    """`ScriptedLLM` that also records the `messages` list of every `complete()` call
    so a test can assert on the exact system prompt the LLM was sent.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(responses)
        self.captured_messages: list[list[Any]] = []

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.captured_messages.append(list(messages))
        return await super().complete(messages, **kwargs)


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"agent_ctx_{suffix}@example.com",
        nombre="Agent Context User",
        cedula=f"4040{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"CTX-{suffix}",
        entidad="Alcaldía de Prueba",
        objeto="Objeto de prueba para el contexto de contrato del agente",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"4040{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_resolve_contrato_context_none_for_none(db: AsyncSession, test_user) -> None:
    user = test_user["user"]
    result = await agent_chat_service._resolve_contrato_context(db, user, None)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_contrato_context_none_for_blank(db: AsyncSession, test_user) -> None:
    user = test_user["user"]
    result = await agent_chat_service._resolve_contrato_context(db, user, "   ")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_contrato_context_none_for_garbage(db: AsyncSession, test_user) -> None:
    user = test_user["user"]
    result = await agent_chat_service._resolve_contrato_context(db, user, "not-a-uuid-at-all")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_contrato_context_none_for_well_formed_but_unknown_uuid(db: AsyncSession, test_user) -> None:
    user = test_user["user"]
    result = await agent_chat_service._resolve_contrato_context(db, user, str(uuid.uuid4()))
    assert result is None


@pytest.mark.asyncio
async def test_resolve_contrato_context_none_for_other_users_contrato(db: AsyncSession) -> None:
    _owner, contrato = await _make_user_with_contrato(db, "0001")
    other_user = Usuario(
        email="agent_ctx_other@example.com",
        nombre="Other User",
        cedula="40409999",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(other_user)
    await db.commit()
    await db.refresh(other_user)

    result = await agent_chat_service._resolve_contrato_context(db, other_user, str(contrato.id))

    assert result is None


@pytest.mark.asyncio
async def test_resolve_contrato_context_returns_block_for_own_contrato(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "0002")

    result = await agent_chat_service._resolve_contrato_context(db, user, str(contrato.id))

    assert result is not None
    assert f"contrato_id={contrato.id}" in result
    assert contrato.numero_contrato in result
    assert "NO lo pidas" in result


@pytest.mark.asyncio
async def test_chat_with_tools_injects_contrato_context_into_system_prompt(db: AsyncSession, monkeypatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "0003")
    scripted = CapturingScriptedLLM([LLMResponse(content="Listo, usé tu contrato.", model="fake", total_tokens=5)])
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(
        db, user, "Crea la cuenta de cobro de febrero", None, {}, contrato_id=str(contrato.id)
    )

    assert result.content == "Listo, usé tu contrato."
    assert len(scripted.captured_messages) == 1
    system_message = scripted.captured_messages[0][0]
    assert system_message.role == "system"
    assert "## Contexto del contrato" in system_message.content
    assert f"contrato_id={contrato.id}" in system_message.content
    assert contrato.numero_contrato in system_message.content


@pytest.mark.asyncio
async def test_chat_with_tools_omits_contrato_context_when_not_provided(
    db: AsyncSession, test_user, monkeypatch
) -> None:
    user = test_user["user"]
    scripted = CapturingScriptedLLM([LLMResponse(content="Hola.", model="fake", total_tokens=5)])
    _patch_llm(monkeypatch, scripted)

    await agent_chat_service.chat_with_tools(db, user, "Hola", None, {})

    # The prompt's static rules always MENTION the "## Contexto del contrato" heading
    # (instructing the model what to do if it's present) — assert on the injected
    # block's actual content instead, which only appears when a context was resolved.
    system_message = scripted.captured_messages[0][0]
    assert "El usuario está trabajando sobre este contrato" not in system_message.content


@pytest.mark.asyncio
async def test_chat_with_tools_ignores_invalid_contrato_id_without_error(
    db: AsyncSession, test_user, monkeypatch
) -> None:
    user = test_user["user"]
    scripted = CapturingScriptedLLM([LLMResponse(content="Hola.", model="fake", total_tokens=5)])
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Hola", None, {}, contrato_id="garbage-not-a-uuid")

    assert result.content == "Hola."
    system_message = scripted.captured_messages[0][0]
    assert "El usuario está trabajando sobre este contrato" not in system_message.content


class TestContratoContextPromptRules:
    """The system prompt must instruct the model to never ask for a raw UUID and to
    self-discover a contract via `listar_contratos`, disambiguating by human terms."""

    def test_prompt_forbids_asking_for_uuid(self) -> None:
        lowered = agent_chat_service.SYSTEM_PROMPT_TEMPLATE.lower()
        assert "uuid" in lowered
        assert "no los conoce" in lowered or "no lo conoce" in lowered

    def test_prompt_mentions_listar_contratos_disambiguation(self) -> None:
        prompt = agent_chat_service.SYSTEM_PROMPT_TEMPLATE
        assert "listar_contratos" in prompt
        assert "NÚMERO DE CONTRATO" in prompt
        assert "listar_cuentas_cobro" in prompt

    def test_prompt_mentions_contrato_context_section(self) -> None:
        prompt = agent_chat_service.SYSTEM_PROMPT_TEMPLATE
        assert "Contexto del contrato" in prompt
