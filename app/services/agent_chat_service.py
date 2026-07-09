"""Agent chat service — free-form, tool-calling agent loop ("Claude-style" chat).

Parallel entrypoint to `agent_service.chat` (which drives the fixed router +
`CompiledGraph` pipeline). Here the LLM decides autonomously, turn by turn, which
registered tools (`app.tools.registry.TOOL_REGISTRY`) to call and in what order to
resolve the user's request end-to-end — importing contracts, creating cuentas de
cobro, managing the document checklist, generating informes, finding evidence, etc.

No credit enforcement happens in this loop: individual tool handlers (e.g.
`crear_cuenta_cobro`, `descubrir_evidencias`) already raise `InsufficientCreditsError`
via their own service-level checks — see `app.tools.catalog`. Re-checking credits
here would duplicate that logic and risk drifting out of sync with it.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm import get_llm
from app.agent.tools.document_parser import parse_document
from app.models.conversacion import Conversacion
from app.models.usuario import Usuario
from app.schemas.agent import AgentChatResult, DocumentoAdjuntoResumen, LLMMessage, ToolEvent
import app.tools.catalog  # noqa: F401 — import-for-side-effect: populates TOOL_REGISTRY
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from app.tools.llm_schema import to_openai_tools
from app.tools.registry import TOOL_REGISTRY

logger = structlog.get_logger("services.agent_chat")

MAX_TOOL_ITERATIONS = 8

# Tool results fed back to the LLM are truncated so a single verbose tool output
# (e.g. a full checklist dump) doesn't blow past the model's context window.
_MAX_TOOL_RESULT_CHARS = 3000

# Per-attachment text excerpt injected into the system prompt.
_MAX_ATTACHMENT_CHARS = 4000

_BINARY_NOTICE = "(contenido binario no extraíble)"

SYSTEM_PROMPT_TEMPLATE = """\
Eres CashIn AI, el asistente de IA de CashIn que ayuda a contratistas colombianos a \
gestionar sus contratos, cuentas de cobro, checklist de documentos, informes y \
evidencias ante entidades públicas.

Hoy es {today}.

Tienes acceso a herramientas para resolver la solicitud del usuario de punta a punta: \
importar contratos, crear y consultar cuentas de cobro, gestionar el checklist de \
documentos requeridos, generar informes, buscar y vincular evidencias, y consultar \
SECOP. Actúa de forma autónoma y encadena las herramientas necesarias antes de \
responder con el resultado final.

Reglas:
- Responde siempre en el mismo idioma en el que te escribe el usuario.
- NUNCA inventes, adivines ni escribas un placeholder o texto descriptivo como valor de un \
argumento UUID (contrato_id, cuenta_id, etc.) — un UUID solo es válido si lo copiaste \
literalmente del resultado JSON de una herramienta que ya llamaste en esta conversación.
- Si para cumplir la solicitud necesitas un UUID que el usuario no escribió explícitamente en \
su mensaje y aún no aparece en un resultado de herramienta anterior, tu ÚNICA llamada en este \
turno debe ser a la herramienta de lectura que lo descubre (`listar_contratos` para un \
contrato_id, `listar_cuentas_cobro` para un cuenta_id) — espera su resultado antes de \
considerar cualquier otra herramienta.
- Ejemplo: para crear una cuenta de cobro cuando el usuario dice "mi contrato" sin dar un ID: \
paso 1) llama a `listar_contratos`; paso 2) toma el campo `id` del contrato correspondiente del \
resultado; paso 3) llama a `crear_cuenta_cobro` con ese `id` exacto como contrato_id.
- Si una herramienta responde con un error de "UUID inválido" o similar, NO te rindas ni \
respondas con texto: significa que usaste un valor inventado. Tu SIGUIENTE llamada debe ser a \
la herramienta de lectura (`listar_contratos` o `listar_cuentas_cobro`) para obtener el UUID \
real, y luego reintentar la operación original con ese valor.
- Si una herramienta responde con un error de "Field required" (falta un argumento), tu \
SIGUIENTE llamada debe repetir la misma operación incluyendo TODOS los argumentos obligatorios \
de su schema (no solo el que faltó) — nunca omitas un argumento obligatorio dos veces seguidas.
- Si el usuario adjuntó archivos en este mensaje, están resumidos más abajo; usa \
`importar_documento` con el nombre exacto del archivo si necesitas procesarlos.
- Sé conciso, directo y profesional."""


def _build_system_prompt(attachment_blocks: list[str]) -> str:
    prompt = SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat())
    if attachment_blocks:
        prompt += "\n\n## Archivos adjuntados en este mensaje\n\n" + "\n\n".join(attachment_blocks)
    return prompt


def _extract_attachment_text(attachment: ToolAttachment) -> str:
    """Best-effort text extraction for the system-prompt preview. Never raises."""
    try:
        text = parse_document(attachment.data, attachment.filename)
    except Exception as exc:
        logger.warning("agent_chat_attachment_parse_failed", filename=attachment.filename, error=str(exc))
        return ""
    return text or ""


def _summarize_tool_result(dumped: Any) -> str:
    """Best-effort short Spanish summary of a tool's dumped output for the trace."""
    if isinstance(dumped, dict):
        for key in ("resumen", "mensaje", "message", "detail"):
            value = dumped.get(key)
            if isinstance(value, str) and value:
                return value
    text = json.dumps(dumped, ensure_ascii=False, default=str)
    return text[:200]


def _serialize_tool_result(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) > _MAX_TOOL_RESULT_CHARS:
        serialized = serialized[:_MAX_TOOL_RESULT_CHARS] + "... (truncado)"
    return serialized


async def _load_or_create_conversation(db: AsyncSession, usuario: Usuario, session_id: str | None) -> Conversacion:
    convo: Conversacion | None = None
    if session_id:
        try:
            parsed = uuid.UUID(session_id)
        except ValueError:
            parsed = None
        if parsed is not None:
            result = await db.execute(
                select(Conversacion).where(
                    Conversacion.id == parsed,
                    Conversacion.usuario_id == usuario.id,
                )
            )
            convo = result.scalar_one_or_none()

    if convo is None:
        convo = Conversacion(usuario_id=usuario.id, mensajes_json=[])
        db.add(convo)
        await db.flush()

    return convo


async def chat_with_tools(
    db: AsyncSession,
    usuario: Usuario,
    message: str,
    session_id: str | None,
    attachments: dict[str, ToolAttachment] | None = None,
) -> AgentChatResult:
    """Run the free-form tool-calling loop for one user message and persist history.

    Tool-call/tool-result messages are exchanged with the LLM within this call only
    — they are never written to `Conversacion.mensajes_json`. Only the user message
    and the final assistant answer are persisted, mirroring `agent_service.chat`.
    """
    attachments = attachments or {}

    convo = await _load_or_create_conversation(db, usuario, session_id)
    # Commit the conversation shell now (not just flush): a tool call raised later in
    # the loop triggers `db.rollback()`, which would otherwise silently discard a
    # brand-new Conversacion row that was only flushed, never committed.
    await db.commit()
    history = [LLMMessage(**m) for m in convo.mensajes_json]

    documentos: list[DocumentoAdjuntoResumen] = []
    attachment_blocks: list[str] = []
    for filename, attachment in attachments.items():
        text = _extract_attachment_text(attachment)
        documentos.append(DocumentoAdjuntoResumen(filename=filename, caracteres_extraidos=len(text)))
        preview = text[:_MAX_ATTACHMENT_CHARS] if text else _BINARY_NOTICE
        attachment_blocks.append(f"### {filename}\n{preview}")

    system_prompt = _build_system_prompt(attachment_blocks)
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=system_prompt),
        *history,
        LLMMessage(role="user", content=message),
    ]

    llm = get_llm()
    tools = to_openai_tools()
    tool_ctx = ToolContext(db=db, usuario=usuario, attachments=attachments)

    tool_events: list[ToolEvent] = []
    tokens_used = 0
    final_content = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await llm.complete(messages, tools=tools, temperature=0.2, max_tokens=1024)
        tokens_used += response.total_tokens

        if not response.tool_calls:
            final_content = response.content
            messages.append(LLMMessage(role="assistant", content=final_content))
            break

        messages.append(
            LLMMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=[
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                    for call in response.tool_calls
                ],
            )
        )

        for call in response.tool_calls:
            spec = TOOL_REGISTRY.get(call.name)
            if spec is None:
                error_detail = f"Unknown tool: {call.name}"
                tool_events.append(ToolEvent(tool=call.name, status="error", resumen=error_detail))
                result_payload: Any = {"error": error_detail}
            else:
                try:
                    output = await invoke_tool(call.name, tool_ctx, call.arguments)
                    if "write" in spec.tags:
                        await db.commit()
                    dumped = output.model_dump(mode="json")
                    result_payload = dumped
                    tool_events.append(
                        ToolEvent(tool=call.name, status="ok", resumen=_summarize_tool_result(dumped))
                    )
                except Exception as exc:
                    # Broad by design: a tool doing real I/O can raise anything (DomainError,
                    # pydantic ValidationError, KeyError/ValueError/TypeError from bad
                    # arguments, but also IntegrityError, httpx errors, RuntimeError, OSError,
                    # etc). Any of these escaping the loop would 500 the whole request and —
                    # since a PRIOR write tool in the same turn may already have committed —
                    # desync those committed side effects from a never-persisted conversation
                    # history. BaseException (KeyboardInterrupt/SystemExit/CancelledError) is
                    # intentionally NOT caught here.
                    await db.rollback()
                    # rollback() expires every object in the session (regardless of
                    # expire_on_commit) — refresh the two long-lived objects the rest
                    # of this loop (and the final persistence step) still reads, so a
                    # later plain attribute access doesn't try a lazy-load outside of
                    # an async-aware context (SQLAlchemy's MissingGreenlet).
                    await db.refresh(usuario)
                    await db.refresh(convo)
                    result_payload = {"error": str(exc)}
                    tool_events.append(ToolEvent(tool=call.name, status="error", resumen=str(exc)))
                    await logger.awarning("agent_chat_tool_error", tool=call.name, error=str(exc))

            messages.append(
                LLMMessage(role="tool", tool_call_id=call.id, content=_serialize_tool_result(result_payload))
            )
    else:
        final_content = (
            "Alcancé el límite de pasos automáticos para esta solicitud. "
            "¿Quieres que continúe con la tarea o prefieres darme más detalles?"
        )
        messages.append(LLMMessage(role="assistant", content=final_content))

    # Re-read the row right before the final write instead of rebuilding from the
    # `history` snapshot taken at function start: two concurrent requests on the same
    # session_id would otherwise have the second writer overwrite (lose) the first
    # writer's messages. Appending to whatever is currently persisted keeps both.
    await db.refresh(convo)
    current_messages = list(convo.mensajes_json or [])
    convo.mensajes_json = [
        *current_messages,
        LLMMessage(role="user", content=message).model_dump(),
        LLMMessage(role="assistant", content=final_content).model_dump(),
    ]
    await db.commit()

    await logger.ainfo(
        "agent_chat_tools",
        session_id=str(convo.id),
        user_id=str(usuario.id),
        tool_calls=len(tool_events),
        tokens_used=tokens_used,
    )

    return AgentChatResult(
        session_id=str(convo.id),
        content=final_content,
        tool_events=tool_events,
        documentos=documentos,
        tokens_used=tokens_used,
    )
