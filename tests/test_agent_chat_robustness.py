"""Tests for hardening the free-form agent chat against weak LOCAL models
(llama3.1:8b via Ollama).

Two real bugs observed live, both reproduced here:

1. The model "draws" a tool call as plain TEXT content instead of emitting a
   real function call — `_recover_tool_calls_from_content` recovers it so the
   loop still executes the tool.
2. A file the user wants imported lives INSIDE a dropped archive (e.g.
   `CUOTA#4.zip` containing `contrato.docx`), not as a top-level attachment —
   `_expand_attachments_for_tools` makes archive members individually
   resolvable by `importar_documento`.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool (importar_documento included)
import pytest
from app.models.documento_fuente import DocumentoFuente
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service
from app.services.agent_chat_service import (
    SYSTEM_PROMPT_TEMPLATE,
    _expand_attachments_for_tools,
    _normalize_tool_args,
    _recover_tool_calls_from_content,
)


class TestNormalizeToolArgs:
    """llama3.1:8b sometimes wraps tool arguments in an OpenAI-ish envelope
    (`{"function": "crear_cuenta_cobro", "parameters": {...}}`) instead of sending
    the real args — those must be unwrapped before invoke_tool."""

    def test_unwraps_function_parameters_envelope(self) -> None:
        assert _normalize_tool_args(
            {"function": "crear_cuenta_cobro", "parameters": {"mes": 2, "anio": 2026}}
        ) == {"mes": 2, "anio": 2026}

    def test_unwraps_name_arguments_envelope(self) -> None:
        assert _normalize_tool_args({"name": "listar_contratos", "arguments": {}}) == {}

    def test_empty_wrapper_collapses_to_empty(self) -> None:
        assert _normalize_tool_args({"function": "listar_contratos"}) == {}

    def test_real_args_pass_through_untouched(self) -> None:
        real = {"contrato_id": "abc", "mes": 2, "anio": 2026}
        assert _normalize_tool_args(real) == real

    def test_mixed_real_and_wrapper_key_not_stripped(self) -> None:
        # A genuine arg alongside a wrapper-looking key must NOT be unwrapped.
        mixed = {"contrato_id": "abc", "parameters": {"mes": 2}}
        assert _normalize_tool_args(mixed) == mixed

    def test_non_dict_returns_empty_dict(self) -> None:
        assert _normalize_tool_args(None) == {}
        assert _normalize_tool_args("nope") == {}


class TestResiliencePrompt:
    """The system prompt must steer the model to ask instead of refusing, and to
    treat informe de supervisión as a real capability (regression: the model replied
    "no puedo generar informes de supervisión")."""

    def test_prompt_forbids_refusing_supported_tasks(self) -> None:
        assert "no puedo" in SYSTEM_PROMPT_TEMPLATE.lower()  # mentioned to forbid it
        assert "informe de supervisión" in SYSTEM_PROMPT_TEMPLATE

    def test_prompt_asks_user_when_blocked(self) -> None:
        lowered = SYSTEM_PROMPT_TEMPLATE.lower()
        assert "pregunta al usuario" in lowered or "pídeselo" in lowered
        assert "resiliente" in lowered and "interactivo" in lowered

    def test_prompt_requires_gathering_contrato_mes_anio_before_crear_cuenta(self) -> None:
        """Regression for the live bug: the model called `crear_cuenta_cobro` with
        `mes`/`anio` missing right after a zip drop. The prompt must explicitly tell
        it to gather contrato_id + mes + anio TOGETHER before that call."""
        lowered = SYSTEM_PROMPT_TEMPLATE.lower()
        assert "antes de llamar a `crear_cuenta_cobro`" in lowered
        assert "contrato_id" in lowered
        assert "mes y anio" in lowered
from app.tools.context import ToolAttachment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_agent_chat_service import ScriptedLLM, _patch_llm


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestRecoverToolCallsFromContent:
    def test_named_shape(self) -> None:
        content = '{"name": "listar_contratos", "arguments": {}}'
        recovered = _recover_tool_calls_from_content(content)
        assert len(recovered) == 1
        assert recovered[0].name == "listar_contratos"
        assert recovered[0].arguments == {}

    def test_fenced_json_block(self) -> None:
        content = (
            "Voy a consultar tus contratos.\n\n"
            "```json\n"
            '{"name": "listar_contratos", "arguments": {}}\n'
            "```\n"
        )
        recovered = _recover_tool_calls_from_content(content)
        assert len(recovered) == 1
        assert recovered[0].name == "listar_contratos"

    def test_bare_args_unique_match(self) -> None:
        # This is the exact live bug: the model draws importar_documento's
        # arguments as plain content instead of a real tool call.
        content = '{"filename": "contrato.docx", "tipo": "contrato", "cuenta_cobro_id": null}'
        recovered = _recover_tool_calls_from_content(content)
        assert len(recovered) == 1
        assert recovered[0].name == "importar_documento"
        assert recovered[0].arguments["filename"] == "contrato.docx"
        assert recovered[0].arguments["tipo"] == "contrato"

    def test_ambiguous_bare_dict_recovers_nothing(self) -> None:
        # {"cuenta_id": ...} alone matches multiple tools identically
        # (resumen_checklist, radicar_cuenta, detectar_desde_secop, ...) —
        # must not guess.
        content = f'{{"cuenta_id": "{uuid.uuid4()}"}}'
        assert _recover_tool_calls_from_content(content) == []

    def test_zero_match_bare_dict_recovers_nothing(self) -> None:
        content = '{"foo": "bar", "unrelated_field": 123}'
        assert _recover_tool_calls_from_content(content) == []

    def test_prose_content_recovers_nothing(self) -> None:
        assert _recover_tool_calls_from_content("Hola, ¿en qué te puedo ayudar hoy?") == []

    def test_malformed_json_recovers_nothing(self) -> None:
        content = "{'filename': 'contrato.docx', tipo: contrato}"  # single quotes / bare keys: invalid JSON
        assert _recover_tool_calls_from_content(content) == []

    def test_empty_content_recovers_nothing(self) -> None:
        assert _recover_tool_calls_from_content("") == []
        assert _recover_tool_calls_from_content("   ") == []


class TestExpandAttachmentsForTools:
    def test_zip_members_become_individually_importable(self) -> None:
        content = _make_zip(
            {
                "contrato.docx": b"fake-docx-bytes",
                "nota.txt": b"nota de prueba",
            }
        )
        attachments = {"CUOTA4.zip": ToolAttachment(filename="CUOTA4.zip", content_type="application/zip", data=content)}

        expanded = _expand_attachments_for_tools(attachments)

        assert "CUOTA4.zip" in expanded  # original preserved
        assert "contrato.docx" in expanded
        assert expanded["contrato.docx"].data == b"fake-docx-bytes"
        assert "nota.txt" in expanded
        assert expanded["nota.txt"].data == b"nota de prueba"

    def test_executable_member_not_exposed(self) -> None:
        content = _make_zip(
            {
                "readme.txt": b"contenido legible",
                "evil.exe": b"MZ\x00\x00\x01\x02binary",
            }
        )
        attachments = {"paquete.zip": ToolAttachment(filename="paquete.zip", content_type="application/zip", data=content)}

        expanded = _expand_attachments_for_tools(attachments)

        assert "readme.txt" in expanded
        assert "evil.exe" not in expanded
        assert not any(key.endswith("evil.exe") for key in expanded)

    def test_name_collision_falls_back_to_prefixed_key(self) -> None:
        content = _make_zip({"notas.txt": b"contenido interno"})
        attachments = {
            # Top-level attachment shares a basename with a zip member.
            "notas.txt": ToolAttachment(filename="notas.txt", content_type="text/plain", data=b"contenido externo"),
            "paquete.zip": ToolAttachment(filename="paquete.zip", content_type="application/zip", data=content),
        }

        expanded = _expand_attachments_for_tools(attachments)

        # Original top-level attachment keeps its plain key.
        assert expanded["notas.txt"].data == b"contenido externo"
        # The colliding zip member falls back to the prefixed key.
        assert "paquete.zip:notas.txt" in expanded
        assert expanded["paquete.zip:notas.txt"].data == b"contenido interno"


@pytest.mark.asyncio
class TestLoopIntegrationRecovery:
    async def test_recovered_bare_args_tool_call_executes_and_loop_continues(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First LLM turn has empty tool_calls but content = the bare-args JSON for a
        REAL tool (importar_documento, matched uniquely via its `filename` field).
        The loop must recover it, actually execute the tool, and continue to a
        second LLM call for the final answer — not treat the raw JSON as the
        final response.
        """
        user = test_user["user"]
        attachment = ToolAttachment(
            filename="instrucciones.txt", content_type="text/plain", data=b"instrucciones de prueba"
        )
        bare_args_content = '{"filename": "instrucciones.txt", "tipo": "instrucciones"}'

        scripted = ScriptedLLM(
            [
                LLMResponse(content=bare_args_content, model="fake", tool_calls=None, total_tokens=15),
                LLMResponse(content="Listo, importé el archivo.", model="fake", total_tokens=10),
            ]
        )
        _patch_llm(monkeypatch, scripted)

        result = await agent_chat_service.chat_with_tools(
            db, user, "Importa el archivo adjunto", None, {"instrucciones.txt": attachment}
        )

        assert scripted.call_count == 2
        assert len(result.tool_events) == 1
        assert result.tool_events[0].tool == "importar_documento"
        assert result.tool_events[0].status == "ok"
        assert result.content == "Listo, importé el archivo."

    async def test_prose_final_turn_after_recovery_is_used_as_final_content(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user = test_user["user"]
        attachment = ToolAttachment(
            filename="instrucciones.txt", content_type="text/plain", data=b"instrucciones de prueba"
        )
        bare_args_content = '{"filename": "instrucciones.txt", "tipo": "instrucciones"}'

        scripted = ScriptedLLM(
            [
                LLMResponse(content=bare_args_content, model="fake", tool_calls=None),
                LLMResponse(content="Documento importado con éxito.", model="fake"),
            ]
        )
        _patch_llm(monkeypatch, scripted)

        result = await agent_chat_service.chat_with_tools(
            db, user, "Importa esto", None, {"instrucciones.txt": attachment}
        )

        assert result.content == "Documento importado con éxito."


@pytest.mark.asyncio
async def test_chat_with_tools_imports_document_from_inside_zip(
    db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end reproduction of the exact live failure: the user drops a zip
    whose MEMBER (contrato.docx) is not a top-level attachment, and the model
    "draws" the tool call as content instead of a real function call. Both
    fixes must combine: recovery finds the tool call, and the archive expansion
    makes `contrato.docx` resolvable inside `ctx.attachments`.
    """
    user = test_user["user"]
    # The "docx" member's bytes must start with the zip magic bytes (PK\x03\x04) —
    # a real .docx IS a zip container — so `importar_documento`'s MIME-signature
    # check accepts it (its actual internal structure doesn't matter here since
    # `parse_document`'s python-docx extraction failure is caught best-effort).
    fake_docx_bytes = _make_zip({"word/document.xml": b"<xml/>"})
    zip_bytes = _make_zip({"contrato.docx": fake_docx_bytes})
    attachment = ToolAttachment(filename="CUOTA#4.zip", content_type="application/zip", data=zip_bytes)

    bare_args_content = '{"filename": "contrato.docx", "tipo": "instrucciones"}'
    scripted = ScriptedLLM(
        [
            LLMResponse(content=bare_args_content, model="fake", tool_calls=None),
            LLMResponse(content="Listo, guardé contrato.docx.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(
        db, user, "Crea una cuenta de cobro con el contrato adjunto", None, {"CUOTA#4.zip": attachment}
    )

    assert result.tool_events[0].tool == "importar_documento"
    assert result.tool_events[0].status == "ok"

    rows = await db.execute(select(DocumentoFuente).where(DocumentoFuente.usuario_id == user.id))
    docs = rows.scalars().all()
    assert len(docs) == 1
    assert docs[0].nombre == "contrato.docx"


@pytest.mark.asyncio
async def test_premature_crear_cuenta_cobro_shows_clean_error_not_pydantic_dump(
    db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact reproduction of the live failure: the model called `crear_cuenta_cobro`
    with `mes`/`anio` missing (only `contrato_id` supplied). The resulting
    `ToolEvent.resumen` shown to the user must be a clean Spanish summary — NEVER a
    raw pydantic dump like "3 validation errors for CuentaCobroCreate ..." — and the
    loop must recover (not 500) and reach a final assistant answer.
    """
    user = test_user["user"]
    tool_call = LLMToolCall(
        id="call_1",
        name="crear_cuenta_cobro",
        arguments={"contrato_id": str(uuid.uuid4())},  # missing mes AND anio
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=10),
            LLMResponse(
                content="¿Para qué mes y año quieres crear la cuenta de cobro?", model="fake", total_tokens=10
            ),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    result = await agent_chat_service.chat_with_tools(db, user, "Creá la cuenta de febrero", None, {})

    assert scripted.call_count == 2
    assert len(result.tool_events) == 1
    event = result.tool_events[0]
    assert event.tool == "crear_cuenta_cobro"
    assert event.status == "error"

    lowered = event.resumen.lower()
    assert "validation error" not in lowered
    assert "pydantic" not in lowered
    assert "field required" not in lowered
    assert "mes" in lowered

    assert result.content == "¿Para qué mes y año quieres crear la cuenta de cobro?"
