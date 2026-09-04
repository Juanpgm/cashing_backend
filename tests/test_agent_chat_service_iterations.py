"""Tests for T8 (raised MAX_TOOL_ITERATIONS) and T9 (cross-turn tool-context
recap) in app/services/agent_chat_service.py.

Root problem: a full radicación chain needs ~12-15 tool calls with the tools
added in this change (listar_contratos → crear_cuenta_cobro →
definir_requisitos_checklist → importar_documento xN → actividades → informes →
resumen_checklist → preparar_radicacion → radicar_cuenta), but the loop capped
at 8 iterations, and continuing a session on the NEXT turn replayed history
with zero memory of which tools already ran or which UUIDs they returned.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy.ext.asyncio import AsyncSession


class ScriptedLLM:
    """Fake LLM adapter — returns pre-scripted `LLMResponse` objects in call order,
    and records every `messages` list it was called with (for T9 assertions)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.messages_seen: list[list[Any]] = []

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        self.messages_seen.append(list(messages))
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self._responses.pop(0)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: ScriptedLLM) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"iter_{suffix}@example.com",
        nombre=f"Iter User {suffix}",
        cedula=f"9191{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"ITER-{suffix}",
        objeto="Objeto de prueba para iteraciones",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"9191{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


def test_max_tool_iterations_raised_to_at_least_20() -> None:
    assert agent_chat_service.MAX_TOOL_ITERATIONS >= 20


@pytest.mark.asyncio
async def test_loop_completes_with_more_than_8_tool_calls(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain of MORE than the old 8-call cap must complete normally (no premature
    'límite de pasos' message) now that the budget was raised."""
    user, contrato = await _make_user_with_contrato(db, "01")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 1, "anio": 2026})
    await db.commit()

    n_calls = 12
    assert n_calls > 8
    responses = [
        LLMResponse(
            content="",
            model="fake",
            tool_calls=[LLMToolCall(id=f"call_{i}", name="resumen_checklist", arguments={"cuenta_id": str(cuenta.id)})],
        )
        for i in range(n_calls)
    ]
    responses.append(LLMResponse(content="Listo, terminé la cadena.", model="fake"))
    scripted = ScriptedLLM(responses)
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Encadena varias consultas", None, {})

    assert scripted.call_count == n_calls + 1
    assert len(result.tool_events) == n_calls
    assert "límite" not in result.content.lower()
    assert result.content == "Listo, terminé la cadena."


@pytest.mark.asyncio
async def test_loop_still_terminates_at_the_new_cap(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "02")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 2, "anio": 2026})
    await db.commit()

    responses = [
        LLMResponse(
            content="",
            model="fake",
            tool_calls=[LLMToolCall(id=f"call_{i}", name="resumen_checklist", arguments={"cuenta_id": str(cuenta.id)})],
        )
        for i in range(agent_chat_service.MAX_TOOL_ITERATIONS)
    ]
    scripted = ScriptedLLM(responses)
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Bucle infinito", None, {})

    assert scripted.call_count == agent_chat_service.MAX_TOOL_ITERATIONS
    assert "límite" in result.content.lower()


@pytest.mark.asyncio
async def test_recap_persisted_and_seen_by_llm_on_next_turn(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "03")

    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 3, "anio": 2026}
    )
    scripted_1 = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Cuenta creada.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted_1)
    result_1 = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de marzo", None, {})
    session_id = result_1.session_id

    convo = await db.get(Conversacion, uuid.UUID(session_id))
    assert convo is not None
    # A recap of the successful tool call must be persisted somewhere in history.
    assert any("crear_cuenta_cobro" in str(m.get("content", "")) for m in convo.mensajes_json)

    scripted_2 = ScriptedLLM([LLMResponse(content="Dale, seguimos.", model="fake")])
    _patch_llm(monkeypatch, scripted_2)
    await agent_chat_service.chat_with_tools(db, user, "Seguí con la cuenta que creamos", session_id, {})

    # The recap from turn 1 must have reached the LLM's messages on turn 2.
    sent_messages = scripted_2.messages_seen[0]
    joined = " ".join(str(getattr(m, "content", "")) for m in sent_messages)
    assert "crear_cuenta_cobro" in joined


@pytest.mark.asyncio
async def test_recap_records_failure_without_fabricating_ids(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, _contrato = await _make_user_with_contrato(db, "04")

    tool_call = LLMToolCall(id="call_1", name="radicar_cuenta", arguments={"cuenta_id": str(uuid.uuid4())})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="No se pudo radicar.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Radica una cuenta inexistente", None, {})

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    recap_messages = [m for m in convo.mensajes_json if "radicar_cuenta" in str(m.get("content", ""))]
    assert recap_messages, "expected a recap entry recording the failed tool call"
    # Never fabricate a real-looking success id for a failed call.
    assert "cuenta_cobro_id=" not in recap_messages[0]["content"]


@pytest.mark.asyncio
async def test_no_tool_calls_persists_no_recap(
    db: AsyncSession, test_user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = test_user["user"]
    scripted = ScriptedLLM([LLMResponse(content="Hola, ¿en qué te ayudo?", model="fake")])
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Hola", None, {})

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    assert len(convo.mensajes_json) == 2  # user + assistant only, no recap message
    for m in convo.mensajes_json:
        assert m["role"] in ("user", "assistant")


@pytest.mark.asyncio
async def test_recap_stays_bounded_after_many_turns(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "05")
    session_id: str | None = None

    for i in range(6):
        tool_call = LLMToolCall(
            id=f"call_{i}",
            name="crear_cuenta_cobro",
            arguments={"contrato_id": str(contrato.id), "mes": (i % 12) + 1, "anio": 2026},
        )
        scripted = ScriptedLLM(
            [
                LLMResponse(content="", model="fake", tool_calls=[tool_call]),
                LLMResponse(content=f"Turno {i} listo.", model="fake"),
            ]
        )
        _patch_llm(monkeypatch, scripted)
        result = await agent_chat_service.chat_with_tools(db, user, f"Turno {i}", session_id, {})
        session_id = result.session_id

    convo = await db.get(Conversacion, uuid.UUID(session_id))
    assert convo is not None
    recap_entries = [
        m
        for m in convo.mensajes_json
        if "crear_cuenta_cobro" in str(m.get("content", "")) and m["role"] not in ("user", "assistant")
    ]
    # Bounded: the recap must be replaced/refreshed each turn, not accumulated one per turn.
    assert len(recap_entries) <= 1
    for m in recap_entries:
        assert len(str(m["content"])) <= 300


@pytest.mark.asyncio
async def test_get_conversation_history_hides_internal_recap(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/v1/chat/{session_id}` (agent_service.get_conversation_history)
    must never leak the internal recap message to the client."""
    from app.services import agent_service

    user, contrato = await _make_user_with_contrato(db, "06")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Cuenta creada.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)
    result = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de junio", None, {})

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    assert any("crear_cuenta_cobro" in str(m.get("content", "")) for m in convo.mensajes_json), (
        "test setup invariant: a recap must actually be persisted for this test to mean anything"
    )

    history = await agent_service.get_conversation_history(db, user.id, uuid.UUID(result.session_id))
    assert all("crear_cuenta_cobro" not in str(m.get("content", "")) for m in history)
    assert all(m.get("role") != "system" for m in history)


@pytest.mark.asyncio
async def test_concurrent_writers_do_not_lose_each_others_messages(
    db: AsyncSession, test_user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the existing `await db.refresh(convo)` re-read pattern:
    two sequential writes to the SAME session must both survive in mensajes_json."""
    user = test_user["user"]

    scripted_1 = ScriptedLLM([LLMResponse(content="Primera respuesta.", model="fake")])
    _patch_llm(monkeypatch, scripted_1)
    result_1 = await agent_chat_service.chat_with_tools(db, user, "Primer mensaje", None, {})

    scripted_2 = ScriptedLLM([LLMResponse(content="Segunda respuesta.", model="fake")])
    _patch_llm(monkeypatch, scripted_2)
    result_2 = await agent_chat_service.chat_with_tools(db, user, "Segundo mensaje", result_1.session_id, {})

    assert result_2.session_id == result_1.session_id
    convo = await db.get(Conversacion, uuid.UUID(result_2.session_id))
    assert convo is not None
    contents = [m["content"] for m in convo.mensajes_json]
    assert "Primer mensaje" in contents
    assert "Primera respuesta." in contents
    assert "Segundo mensaje" in contents
    assert "Segunda respuesta." in contents


def test_build_tool_context_recap_caps_the_first_line_too() -> None:
    """Regression: `if lines and budget + added > _RECAP_MAX_CHARS` skipped the
    length check entirely for the FIRST line (since `lines` is empty on the
    first iteration) — a single oversized result could blow the cap outright."""
    dumped = {f"campo_{i}_id": str(uuid.uuid4()) for i in range(20)}
    call_results = [("una_herramienta_con_muchos_ids", "ok", dumped)]

    recap = agent_chat_service._build_tool_context_recap(call_results)

    assert recap is not None
    assert len(recap) <= agent_chat_service._RECAP_MAX_CHARS
