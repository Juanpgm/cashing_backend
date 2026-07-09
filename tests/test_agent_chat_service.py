"""Tests for app.services.agent_chat_service — the free-form tool-calling agent loop.

The LLM is fully scripted (`ScriptedLLM`) — no network calls. Real registry tools
(seeded via `app.tools.catalog`) are invoked through the actual loop against the
in-memory SQLite `db` fixture to prove commit/rollback and tool_events behavior
end-to-end.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool (importar_documento included)
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_fuente import DocumentoFuente
from app.models.usuario import Usuario
from app.schemas.agent import DocumentoAdjuntoResumen, LLMResponse, LLMToolCall
from app.services import agent_chat_service
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ScriptedLLM:
    """Fake LLM adapter — returns pre-scripted `LLMResponse` objects in call order."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self._responses.pop(0)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: ScriptedLLM) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


async def _make_user_with_contrato(db: AsyncSession, suffix: str = "0001") -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"agent_chat_{suffix}@example.com",
        nombre="Agent Chat User",
        cedula=f"3030{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"AC-{suffix}",
        objeto="Objeto de prueba para el agente conversacional",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"3030{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_direct_answer_no_tools_persists_conversation(db: AsyncSession, test_user, monkeypatch) -> None:
    user = test_user["user"]
    scripted = ScriptedLLM([LLMResponse(content="Hola, ¿en qué te ayudo?", model="fake", total_tokens=42)])
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Hola", None, {})

    assert result.content == "Hola, ¿en qué te ayudo?"
    assert result.tool_events == []
    assert result.documentos == []
    assert result.tokens_used == 42
    assert scripted.call_count == 1

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    assert len(convo.mensajes_json) == 2
    assert convo.mensajes_json[0]["role"] == "user"
    assert convo.mensajes_json[0]["content"] == "Hola"
    assert convo.mensajes_json[1]["role"] == "assistant"
    assert convo.mensajes_json[1]["content"] == "Hola, ¿en qué te ayudo?"


@pytest.mark.asyncio
async def test_write_tool_call_commits_and_records_event(db: AsyncSession, monkeypatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "0002")
    tool_call = LLMToolCall(
        id="call_1",
        name="crear_cuenta_cobro",
        arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026},
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=10),
            LLMResponse(content="Listo, creé tu cuenta de cobro de junio 2026.", model="fake", total_tokens=20),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de junio", None, {})

    assert result.content == "Listo, creé tu cuenta de cobro de junio 2026."
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool == "crear_cuenta_cobro"
    assert result.tool_events[0].status == "ok"
    assert result.tokens_used == 30

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    cuentas = rows.scalars().all()
    assert len(cuentas) == 1
    assert cuentas[0].mes == 6
    assert cuentas[0].anio == 2026


@pytest.mark.asyncio
async def test_tool_domain_error_recorded_and_loop_continues(db: AsyncSession, monkeypatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "0003")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 7, "anio": 2026}
    )
    await db.commit()

    tool_call = LLMToolCall(id="call_1", name="radicar_cuenta", arguments={"cuenta_id": str(cuenta.id)})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="No se pudo radicar porque falta completar el checklist.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Radica mi cuenta", None, {})

    assert scripted.call_count == 2
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool == "radicar_cuenta"
    assert result.tool_events[0].status == "error"
    assert result.content == "No se pudo radicar porque falta completar el checklist."


@pytest.mark.asyncio
async def test_max_tool_iterations_cap_stops_and_warns(db: AsyncSession, monkeypatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "0004")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 8, "anio": 2026}
    )
    await db.commit()

    responses = [
        LLMResponse(
            content="",
            model="fake",
            tool_calls=[
                LLMToolCall(id=f"call_{i}", name="resumen_checklist", arguments={"cuenta_id": str(cuenta.id)})
            ],
        )
        for i in range(agent_chat_service.MAX_TOOL_ITERATIONS)
    ]
    scripted = ScriptedLLM(responses)
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Revisa mi checklist en bucle", None, {})

    assert scripted.call_count == agent_chat_service.MAX_TOOL_ITERATIONS
    assert len(result.tool_events) == agent_chat_service.MAX_TOOL_ITERATIONS
    assert all(ev.status == "ok" for ev in result.tool_events)
    assert "límite" in result.content.lower()


@pytest.mark.asyncio
async def test_importar_documento_via_attachment_creates_real_document(
    db: AsyncSession, test_user, monkeypatch
) -> None:
    user = test_user["user"]
    content = b"Estas son las instrucciones de prueba para el agente conversacional."
    attachment = ToolAttachment(filename="instrucciones.txt", content_type="text/plain", data=content)

    tool_call = LLMToolCall(
        id="call_1",
        name="importar_documento",
        arguments={"filename": "instrucciones.txt", "tipo": "instrucciones"},
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Documento importado correctamente.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(
        db, user, "Importa el archivo adjunto", None, {"instrucciones.txt": attachment}
    )

    assert result.tool_events[0].tool == "importar_documento"
    assert result.tool_events[0].status == "ok"
    # parse_document only supports .pdf/.docx/.xlsx/.xls (see app/agent/tools/document_parser.py)
    # — a .txt attachment is a legitimate "non-parseable" case, hence 0 extracted chars here.
    assert result.documentos == [DocumentoAdjuntoResumen(filename="instrucciones.txt", caracteres_extraidos=0)]

    rows = await db.execute(select(DocumentoFuente).where(DocumentoFuente.usuario_id == user.id))
    docs = rows.scalars().all()
    assert len(docs) == 1
    assert docs[0].nombre == "instrucciones.txt"


@pytest.mark.asyncio
async def test_tool_runtime_error_is_caught_and_loop_continues(db: AsyncSession, monkeypatch) -> None:
    """A tool raising a non-domain exception (e.g. real I/O failure) must not 500 the
    request: the broadened per-tool-call exception boundary should catch it, roll back,
    record an error ToolEvent, and let the loop continue to a final assistant answer.
    """
    user, contrato = await _make_user_with_contrato(db, "0005")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 9, "anio": 2026}
    )
    await db.commit()

    tool_call = LLMToolCall(id="call_1", name="resumen_checklist", arguments={"cuenta_id": str(cuenta.id)})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Ocurrió un error inesperado al consultar el checklist.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    async def _raise_runtime_error(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom - simulated I/O failure")

    monkeypatch.setattr(agent_chat_service, "invoke_tool", _raise_runtime_error)

    result = await agent_chat_service.chat_with_tools(db, user, "Resume mi checklist", None, {})

    assert scripted.call_count == 2
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool == "resumen_checklist"
    assert result.tool_events[0].status == "error"
    assert result.content == "Ocurrió un error inesperado al consultar el checklist."

    convo = await db.get(Conversacion, uuid.UUID(result.session_id))
    assert convo is not None
    assert convo.mensajes_json[-1]["content"] == "Ocurrió un error inesperado al consultar el checklist."


@pytest.mark.asyncio
async def test_sequential_calls_append_without_clobbering_history(db: AsyncSession, test_user, monkeypatch) -> None:
    """Regression for the lost-update bug: the final write used to overwrite
    `mensajes_json` wholesale from a snapshot taken at function start. The fix
    re-reads the row right before the final write and appends. A second sequential
    call on the same session must grow the history by 2, never clobber it.
    """
    user = test_user["user"]
    seeded = [
        {"role": "user", "content": "Mensaje previo 1"},
        {"role": "assistant", "content": "Respuesta previa 1"},
    ]
    convo = Conversacion(usuario_id=user.id, mensajes_json=list(seeded))
    db.add(convo)
    await db.commit()
    await db.refresh(convo)
    session_id = str(convo.id)

    scripted1 = ScriptedLLM([LLMResponse(content="Primera respuesta nueva.", model="fake", total_tokens=5)])
    _patch_llm(monkeypatch, scripted1)
    await agent_chat_service.chat_with_tools(db, user, "Pregunta 1", session_id, {})

    convo_after_1 = await db.get(Conversacion, uuid.UUID(session_id))
    assert convo_after_1 is not None
    assert len(convo_after_1.mensajes_json) == len(seeded) + 2
    assert convo_after_1.mensajes_json[: len(seeded)] == seeded
    assert convo_after_1.mensajes_json[-2]["content"] == "Pregunta 1"
    assert convo_after_1.mensajes_json[-1]["content"] == "Primera respuesta nueva."

    scripted2 = ScriptedLLM([LLMResponse(content="Segunda respuesta nueva.", model="fake", total_tokens=5)])
    _patch_llm(monkeypatch, scripted2)
    await agent_chat_service.chat_with_tools(db, user, "Pregunta 2", session_id, {})

    convo_after_2 = await db.get(Conversacion, uuid.UUID(session_id))
    assert convo_after_2 is not None
    assert len(convo_after_2.mensajes_json) == len(seeded) + 4
    assert convo_after_2.mensajes_json[: len(seeded)] == seeded
    assert convo_after_2.mensajes_json[-2]["content"] == "Pregunta 2"
    assert convo_after_2.mensajes_json[-1]["content"] == "Segunda respuesta nueva."


@pytest.mark.asyncio
async def test_unknown_tool_name_records_error_and_continues(db: AsyncSession, test_user, monkeypatch) -> None:
    user = test_user["user"]
    tool_call = LLMToolCall(id="call_1", name="not_a_real_tool", arguments={})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="No pude ejecutar esa acción.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Haz algo raro", None, {})

    assert scripted.call_count == 2
    assert result.tool_events[0].tool == "not_a_real_tool"
    assert result.tool_events[0].status == "error"
    assert result.content == "No pude ejecutar esa acción."
