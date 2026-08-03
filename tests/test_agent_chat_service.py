"""Tests for app.services.agent_chat_service — the free-form tool-calling agent loop.

The LLM is fully scripted (`ScriptedLLM`) — no network calls. Real registry tools
(seeded via `app.tools.catalog`) are invoked through the actual loop against the
in-memory SQLite `db` fixture to prove commit/rollback and tool_events behavior
end-to-end.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool (importar_documento included)
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.documento_cuenta_cobro import EstadoRequisito
from app.models.documento_fuente import DocumentoFuente
from app.models.usuario import Usuario
from app.schemas.agent import DocumentoAdjuntoResumen, LLMResponse, LLMToolCall, UiAction
from app.schemas.checklist import ChecklistResponse, ChecklistResumen, RequisitoCatalogoOut, RequisitoChecklistItem
from app.schemas.cuenta_cobro import CuentaCobroResponse
from app.schemas.google_workspace import EvidenceDiscoveryResponse, ObligacionJustificada
from app.schemas.paquete import PaqueteGeneradoResponse
from app.schemas.radicacion_prep import PreparaRadicacionResponse
from app.services import agent_chat_service
from app.tools.catalog.listar_contratos import ContratoResumen, ListarContratosOutput
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
    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 7, "anio": 2026})
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
    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 8, "anio": 2026})
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
    # parse_document now decodes plain text (and expands archives), so a .txt
    # attachment reports its extracted character count instead of 0.
    assert result.documentos == [
        DocumentoAdjuntoResumen(filename="instrucciones.txt", caracteres_extraidos=len(content))
    ]

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
    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 9, "anio": 2026})
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


@pytest.mark.asyncio
async def test_ui_actions_derived_from_successful_tool_calls(db: AsyncSession, monkeypatch) -> None:
    """Two chained successful tool calls (listar_contratos -> crear_cuenta_cobro)
    each produce one deterministic UiAction, in call order."""
    user, contrato = await _make_user_with_contrato(db, "0006")
    list_call = LLMToolCall(id="call_1", name="listar_contratos", arguments={})
    create_call = LLMToolCall(
        id="call_2",
        name="crear_cuenta_cobro",
        arguments={"contrato_id": str(contrato.id), "mes": 5, "anio": 2026},
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[list_call]),
            LLMResponse(content="", model="fake", tool_calls=[create_call]),
            LLMResponse(content="Listo, aquí tienes tu cuenta de mayo.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de mayo", None, {})

    assert len(result.ui_actions) == 2
    seleccionar, abrir = result.ui_actions
    assert seleccionar.type == "seleccionar_contrato"
    assert seleccionar.payload["contratos"][0]["id"] == str(contrato.id)
    assert seleccionar.payload["contratos"][0]["numero_contrato"] == contrato.numero_contrato
    assert abrir.type == "abrir_radicacion"
    assert abrir.payload["mes"] == 5
    assert abrir.payload["anio"] == 2026
    assert "cuenta_id" in abrir.payload


@pytest.mark.asyncio
async def test_ui_action_builder_exception_is_skipped(db: AsyncSession, monkeypatch) -> None:
    """A builder raising must be logged+skipped — the tool call itself still
    succeeds and the chat response is still returned normally, just without
    that particular UiAction."""
    user, contrato = await _make_user_with_contrato(db, "0007")
    tool_call = LLMToolCall(
        id="call_1",
        name="crear_cuenta_cobro",
        arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026},
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Listo.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    def _boom(output: Any) -> Any:
        raise RuntimeError("builder boom")

    monkeypatch.setitem(agent_chat_service._UI_ACTION_BUILDERS, "crear_cuenta_cobro", _boom)

    result = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de junio", None, {})

    assert result.content == "Listo."
    assert result.ui_actions == []
    assert result.tool_events[0].status == "ok"


# --- W1: direct unit tests for the 5 previously-untested `_UI_ACTION_BUILDERS`
# entries (resumen_checklist, descubrir_evidencias, preparar_radicacion,
# generar_paquete_evidencias, radicar_cuenta). `_run_ui_action_builder` swallows
# any builder exception (see test above), so these are the only signal that a
# schema change silently breaks a builder's field access — that's the regression
# these guard against.


def _make_cuenta_cobro_response(estado: EstadoCuentaCobro) -> CuentaCobroResponse:
    now = datetime(2026, 6, 1)
    return CuentaCobroResponse(
        id=uuid.uuid4(),
        contrato_id=uuid.uuid4(),
        mes=6,
        anio=2026,
        estado=estado,
        valor=Decimal("1000000.00"),
        pdf_storage_key=None,
        fecha_envio=None,
        numero_cuota=1,
        posicion=PosicionCuota.PRIMERA,
        informe_final=False,
        fecha_transaccion=None,
        contexto_usuario=None,
        actividades=[],
        created_at=now,
        updated_at=now,
    )


def test_build_checklist_resumen_derives_payload_fields_and_values() -> None:
    cuenta_id = uuid.uuid4()
    requisito = RequisitoCatalogoOut(
        codigo="RUT",
        etiqueta="RUT vigente",
        obligatorio=True,
        solo_primera_cuenta=False,
        permite_autogen=False,
        orden=1,
    )
    item = RequisitoChecklistItem(requisito=requisito, estado=EstadoRequisito.CARGADO)
    resumen = ChecklistResumen(total=1, cumplidos=1, pendientes=0, lista_pendientes=["RUT"], radicacion_lista=False)
    output = ChecklistResponse(cuenta_cobro_id=cuenta_id, items=[item], resumen=resumen)

    action = agent_chat_service._build_checklist_resumen(output)

    assert action is not None
    assert action.type == "checklist_resumen"
    assert action.payload == {
        "cuenta_id": str(cuenta_id),
        "cumplidos": 1,
        "pendientes": 0,
        "lista_pendientes": ["RUT"],
        "items": [{"codigo": "RUT", "etiqueta": "RUT vigente", "estado": "cargado"}],
    }


def test_build_evidencias_descubiertas_derives_payload_fields_and_values() -> None:
    obligacion = ObligacionJustificada(
        obligacion_id=str(uuid.uuid4()),
        descripcion="Entregar informe mensual",
        justificacion="Se entregó el informe vía correo el 5 de junio.",
    )
    output = EvidenceDiscoveryResponse(
        obligaciones=[obligacion],
        resumen="Se encontraron 3 evidencias en el correo y Drive.",
        total_evidencias=3,
        fuentes={"email": 2, "drive": 1},
    )

    action = agent_chat_service._build_evidencias_descubiertas(output)

    assert action is not None
    assert action.type == "evidencias_descubiertas"
    assert action.payload == {
        "total_evidencias": 3,
        "obligaciones_justificadas": 1,
        "fuentes": {"email": 2, "drive": 1},
        "resumen": "Se encontraron 3 evidencias en el correo y Drive.",
    }


def test_build_paquete_listo_desde_preparacion_derives_payload_fields_and_values() -> None:
    cuenta_id = uuid.uuid4()
    output = PreparaRadicacionResponse(
        cuenta_cobro_id=cuenta_id,
        storage_key="cuentas/abc/paquete.zip",
        filename="paquete_junio_2026.zip",
        size_bytes=1024,
        listo_para_radicar=True,
        pendientes=0,
        es_borrador=False,
    )

    action = agent_chat_service._build_paquete_listo_desde_preparacion(output)

    assert action is not None
    assert action.type == "paquete_listo"
    assert action.payload == {
        "cuenta_id": str(cuenta_id),
        "filename": "paquete_junio_2026.zip",
        "listo_para_radicar": True,
        "pendientes": 0,
    }


def test_build_paquete_listo_desde_paquete_derives_listo_from_pendientes_zero() -> None:
    """`PaqueteGeneradoResponse` (unlike `PreparaRadicacionResponse`) has no
    `listo_para_radicar` field — the builder must derive it from `pendientes == 0`."""
    cuenta_id = uuid.uuid4()
    output = PaqueteGeneradoResponse(
        cuenta_cobro_id=cuenta_id,
        storage_key="cuentas/abc/paquete.zip",
        filename="paquete.zip",
        size_bytes=2048,
        pendientes=0,
        modo="completo",
    )

    action = agent_chat_service._build_paquete_listo_desde_paquete(output)

    assert action is not None
    assert action.type == "paquete_listo"
    assert action.payload == {
        "cuenta_id": str(cuenta_id),
        "filename": "paquete.zip",
        "listo_para_radicar": True,
        "pendientes": 0,
    }


def test_build_paquete_listo_desde_paquete_not_listo_when_pendientes_positive() -> None:
    output = PaqueteGeneradoResponse(
        cuenta_cobro_id=uuid.uuid4(),
        storage_key="cuentas/xyz/paquete.zip",
        filename="paquete.zip",
        size_bytes=2048,
        pendientes=2,
        modo="borrador",
    )

    action = agent_chat_service._build_paquete_listo_desde_paquete(output)

    assert action is not None
    assert action.payload["listo_para_radicar"] is False
    assert action.payload["pendientes"] == 2


def test_build_cuenta_radicada_derives_payload_fields_and_values() -> None:
    output = _make_cuenta_cobro_response(EstadoCuentaCobro.ENVIADA)

    action = agent_chat_service._build_cuenta_radicada(output)

    assert action is not None
    assert action.type == "cuenta_radicada"
    assert action.payload == {"cuenta_id": str(output.id), "estado": "enviada"}


# --- W2: `_record_ui_action` invariants — consecutive-same-type collapsing,
# the `_MAX_UI_ACTIONS` cap dropping the oldest entry, and the cap's independence
# from `MAX_TOOL_ITERATIONS`.


def test_record_ui_action_replaces_consecutive_same_type() -> None:
    ui_actions: list[UiAction] = []
    first = UiAction(type="checklist_resumen", payload={"cumplidos": 1})
    second = UiAction(type="checklist_resumen", payload={"cumplidos": 2})

    agent_chat_service._record_ui_action(ui_actions, first)
    agent_chat_service._record_ui_action(ui_actions, second)

    assert len(ui_actions) == 1
    assert ui_actions[0] is second


def test_record_ui_action_drops_oldest_beyond_cap() -> None:
    """9 alternating-type actions (so none collapse into each other) must leave
    exactly `_MAX_UI_ACTIONS` entries, with the single oldest one dropped."""
    ui_actions: list[UiAction] = []
    actions = [
        UiAction(type="checklist_resumen" if i % 2 == 0 else "abrir_radicacion", payload={"i": i}) for i in range(9)
    ]

    for action in actions:
        agent_chat_service._record_ui_action(ui_actions, action)

    assert len(ui_actions) == agent_chat_service._MAX_UI_ACTIONS
    assert ui_actions == actions[1:]


def test_max_ui_actions_cap_independent_of_max_tool_iterations() -> None:
    """Documents the invariant `_record_ui_action`'s cap relies on staying safe:
    `MAX_TOOL_ITERATIONS` (successful tool calls per chat turn) must never exceed
    `_MAX_UI_ACTIONS`, otherwise the cap alone — not the loop's own iteration
    bound — is what keeps `ui_actions` finite. Also proves the cap holds even
    when fed more actions than `MAX_TOOL_ITERATIONS` would ever produce today."""
    assert agent_chat_service.MAX_TOOL_ITERATIONS <= agent_chat_service._MAX_UI_ACTIONS

    ui_actions: list[UiAction] = []
    total = agent_chat_service._MAX_UI_ACTIONS + agent_chat_service.MAX_TOOL_ITERATIONS
    for i in range(total):
        action = UiAction(type="checklist_resumen" if i % 2 == 0 else "abrir_radicacion", payload={"i": i})
        agent_chat_service._record_ui_action(ui_actions, action)

    assert len(ui_actions) == agent_chat_service._MAX_UI_ACTIONS


# --- W3: `_serialize_tool_result` truncation-limit regression + compact
# serializers for the two chatty list tools (`listar_contratos`,
# `resumen_checklist`). Live bug this guards against: at the old 3000-char
# limit, a 9-contrato `listar_contratos` result got cut off mid-array so the
# LLM answered "no encontré ese contrato" for one that WAS in the (truncated)
# list, and a `resumen_checklist` dump got cut off so the LLM told the user
# the checklist was truncated and it couldn't continue.


def _make_contrato_resumen(numero: str) -> ContratoResumen:
    return ContratoResumen(
        id=uuid.uuid4(),
        numero_contrato=numero,
        entidad=f"Entidad {numero}",
        objeto="Objeto de prueba",
        valor_mensual=Decimal("1000000.00"),
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
    )


def test_compact_listar_contratos_includes_last_of_nine() -> None:
    """Regression for the observed truncation bug: with 9 contratos, the LAST
    one (the one the old 3000-char generic JSON truncation cut off) must be
    present in the compact serialization fed to the LLM."""
    contratos = [_make_contrato_resumen(f"AC-000{i}") for i in range(9)]
    output = ListarContratosOutput(contratos=contratos)

    text = agent_chat_service._compact_listar_contratos(output)
    lines = text.splitlines()

    assert len(lines) == 9
    last = contratos[-1]
    assert f"{last.numero_contrato} | {last.entidad} | {last.valor_mensual} | {last.id}" in text
    # Sanity: the serialized form fits comfortably inside the new bound, unlike
    # the generic `model_dump` JSON of the same 9 contratos would.
    assert len(text) < agent_chat_service._MAX_TOOL_RESULT_CHARS


def test_compact_resumen_checklist_includes_every_item_codigo() -> None:
    codigos = [f"REQ-{i:02d}" for i in range(12)]
    items = [
        RequisitoChecklistItem(
            requisito=RequisitoCatalogoOut(
                codigo=codigo,
                etiqueta=f"Etiqueta {codigo}",
                obligatorio=True,
                solo_primera_cuenta=False,
                permite_autogen=False,
                orden=i,
            ),
            estado=EstadoRequisito.PENDIENTE if i % 2 == 0 else EstadoRequisito.CARGADO,
        )
        for i, codigo in enumerate(codigos)
    ]
    resumen = ChecklistResumen(
        total=12, cumplidos=6, pendientes=6, lista_pendientes=codigos[::2], radicacion_lista=False
    )
    output = ChecklistResponse(cuenta_cobro_id=uuid.uuid4(), items=items, resumen=resumen)

    text = agent_chat_service._compact_resumen_checklist(output)

    for codigo in codigos:
        assert codigo in text
    assert "total=12" in text
    assert "cumplidos=6" in text


def test_serialize_tool_result_uses_compact_serializer_when_registered() -> None:
    contratos = [_make_contrato_resumen(f"AC-000{i}") for i in range(9)]
    output = ListarContratosOutput(contratos=contratos)
    dumped = output.model_dump(mode="json")

    result = agent_chat_service._serialize_tool_result(dumped, "listar_contratos", output)

    assert result == agent_chat_service._compact_listar_contratos(output)
    assert "... (truncado)" not in result


def test_serialize_tool_result_generic_path_still_truncates_at_new_limit() -> None:
    """A tool with no compact serializer registered must still fall back to the
    generic `model_dump` + truncate path, now bounded at the NEW limit (12000),
    with the truncation marker preserved."""
    huge_payload = {"detalle": "x" * (agent_chat_service._MAX_TOOL_RESULT_CHARS * 2)}

    result = agent_chat_service._serialize_tool_result(huge_payload, "algun_otro_tool", None)

    assert result.endswith("... (truncado)")
    assert len(result) == agent_chat_service._MAX_TOOL_RESULT_CHARS + len("... (truncado)")


def test_serialize_tool_result_falls_back_to_generic_when_compact_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compact serializer raising must not blow up the tool loop — fall back to
    the generic path instead of propagating the exception."""

    def _boom(output: Any) -> str:
        raise RuntimeError("compact boom")

    monkeypatch.setitem(agent_chat_service._COMPACT_SERIALIZERS, "listar_contratos", _boom)
    output = ListarContratosOutput(contratos=[_make_contrato_resumen("AC-0001")])
    dumped = output.model_dump(mode="json")

    result = agent_chat_service._serialize_tool_result(dumped, "listar_contratos", output)

    assert result == json.dumps(dumped, ensure_ascii=False, default=str)
