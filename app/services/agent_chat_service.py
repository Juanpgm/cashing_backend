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
import mimetypes
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.catalog  # noqa: F401 — import-for-side-effect: populates TOOL_REGISTRY
from app.adapters.llm import get_llm
from app.agent.tools.document_parser import (
    _NON_TEXT_EXTENSIONS,
    is_archive_filename,
    iter_archive_members,
    parse_document,
)
from app.core.exceptions import DomainError
from app.models.conversacion import Conversacion
from app.models.usuario import Usuario
from app.schemas.agent import AgentChatResult, DocumentoAdjuntoResumen, LLMMessage, LLMToolCall, ToolEvent
from app.services import contrato_service
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

# Total archive members exposed as importable attachments across ALL archive
# attachments in one turn (in addition to `iter_archive_members`'s own per-archive
# cap) — bounds the tool context a single message can inject.
_MAX_EXPANDED_ATTACHMENTS = 50

# Matches ```json ... ``` or bare ``` ... ``` fenced code blocks (case-insensitive
# language tag), used to recover a tool call a weak local model "drew" as prose
# instead of a real function call.
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT_TEMPLATE = """\
Eres CashIn AI, el asistente de IA de CashIn que ayuda a contratistas colombianos a \
gestionar sus contratos, cuentas de cobro, checklist de documentos, informes y \
evidencias ante entidades públicas.

Hoy es {today}.

Tienes acceso a herramientas para resolver la solicitud del usuario de punta a punta: \
importar contratos, crear y consultar cuentas de cobro, gestionar el checklist de \
documentos requeridos, generar informes (tanto el informe de actividades como el \
informe de supervisión), buscar y vincular evidencias, y consultar SECOP. Actúa de \
forma autónoma y encadena las herramientas necesarias antes de responder con el \
resultado final.

El informe de supervisión SÍ se puede generar: es el mismo contenido del informe de \
actividades (las mismas obligaciones, actividades y justificaciones) presentado en \
formato y tono de supervisor. Usa la herramienta `generar_informe_supervision` para \
producirlo; NUNCA afirmes que no puedes generarlo.

Reglas:
- Responde siempre en el mismo idioma en el que te escribe el usuario.
- NUNCA respondas con un "lo siento, no puedo" ni te niegues a una tarea que tus \
herramientas cubren (crear cuentas de cobro, checklist, informes de actividades y de \
supervisión, evidencias, SECOP, importar documentos). Si tienes una herramienta para \
eso, úsala.
- Sé RESILIENTE e INTERACTIVO: si te falta un dato para continuar (cuál cuenta de \
cobro, cuál contrato, el período, un valor, etc.) y no puedes descubrirlo con una \
herramienta de lectura (`listar_contratos`, `listar_cuentas_cobro`, \
`resumen_checklist`), PREGUNTA al usuario de forma concreta qué necesitas — nunca te \
rindas ni inventes datos.
- Si una herramienta falla con un error que describe una condición previa (por \
ejemplo "define primero el checklist", "no hay actividades registradas"), NO te \
detengas con una disculpa: explícale al usuario en lenguaje simple qué falta, ofrécete \
a resolverlo con las herramientas disponibles y, si necesitas su confirmación o un \
dato, pídeselo con una pregunta clara.
- Cuando no logres completar algo, tu respuesta final debe decir qué intentaste, qué \
te bloqueó y qué necesitas del usuario para continuar — en forma de pregunta accionable, \
no de negativa.
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
- Antes de llamar a `crear_cuenta_cobro`, reúne SIEMPRE los tres datos juntos: contrato_id \
(descubierto con `listar_contratos`, nunca nulo), mes y anio. Si el usuario solo dijo el mes \
(por ejemplo "febrero") y no el año, asume el año en curso solo si es evidente por el contexto; \
si falta algún dato que no puedes descubrir con una herramienta de lectura, PREGÚNTALO antes de \
llamar la herramienta — nunca la llames con datos incompletos.
- NUNCA le pidas al usuario un `contrato_id`, `cuenta_id` ni ningún UUID/ID interno — el \
usuario no los conoce ni debería tener que conocerlos. Si necesitas uno, descúbrelo con las \
herramientas de lectura; jamás lo preguntes como si fuera un dato que el usuario pudiera darte.
- Para obtener un contrato: si más abajo hay una sección "## Contexto del contrato", usa ESE \
contrato_id directamente sin llamar a ninguna herramienta para buscarlo. Si no hay esa sección, \
llama a `listar_contratos`: si devuelve exactamente un contrato, úsalo sin preguntar nada; si \
devuelve varios, pregúntale al usuario cuál usar identificándolo por su NÚMERO DE CONTRATO, \
ENTIDAD u OBJETO (nunca por UUID) y espera su respuesta antes de continuar.
- Lo mismo aplica para la cuenta de cobro: descúbrela con `listar_cuentas_cobro`; si hay que \
elegir entre varias, pregunta por mes/año/estado — nunca por su id.
- Si el usuario adjuntó documentos y en su texto o en el documento aparece un número de \
contrato, puedes llamar a `listar_contratos` y hacer coincidir por ese número para identificar \
el contrato correcto — sin pedirle el id en ningún momento.
- Si el usuario adjuntó archivos en este mensaje, están resumidos más abajo; su \
contenido de texto ya fue extraído (incluye archivos comprimidos .zip/.tar.gz, cuyo \
contenido se expande archivo por archivo, y formatos de texto como .txt/.csv/.md/.json). \
Usa `importar_documento` con el nombre exacto del archivo si necesitas guardarlo como \
documento del contrato o del checklist.
- Si el usuario adjuntó un archivo comprimido (.zip/.tar.gz/.tgz), sus archivos internos \
también son importables de forma individual por su nombre exacto — revisa la lista de \
"Archivos que puedes importar por nombre exacto" para saber cuáles.
- SIEMPRE invoca las herramientas a través del mecanismo de function-calling del modelo. \
NUNCA escribas el nombre de una herramienta ni sus argumentos en formato JSON como texto \
plano en tu respuesta — eso no ejecuta nada; si necesitas llamar a una herramienta, hazlo \
mediante una llamada de función real, nunca describiéndola en el contenido del mensaje.
- Sé conciso, directo y profesional."""


_MAX_OBJETO_CONTEXT_CHARS = 200


def _build_system_prompt(
    attachment_blocks: list[str],
    importable_filenames: list[str] | None = None,
    contrato_context: str | None = None,
) -> str:
    prompt = SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat())
    if contrato_context:
        prompt += "\n\n## Contexto del contrato\n\n" + contrato_context
    if attachment_blocks:
        prompt += "\n\n## Archivos adjuntados en este mensaje\n\n" + "\n\n".join(attachment_blocks)
    if importable_filenames:
        nombres = ", ".join(f"`{name}`" for name in importable_filenames)
        prompt += f"\n\nArchivos que puedes importar por nombre exacto: {nombres}."
    return prompt


async def _resolve_contrato_context(db: AsyncSession, usuario: Usuario, contrato_id: str | None) -> str | None:
    """Resolve an OPTIONAL contract context passed by the frontend into a system-prompt block.

    Never raises: a missing/blank/malformed UUID, an unknown contrato_id, or one not
    owned by `usuario` all resolve to `None` (the agent falls back to discovering the
    contract itself via `listar_contratos`) — this is a convenience hint, not a
    trust boundary the rest of the request depends on.
    """
    if not contrato_id or not contrato_id.strip():
        return None

    try:
        parsed_id = uuid.UUID(contrato_id.strip())
    except ValueError:
        await logger.awarning("agent_chat_contrato_context_invalid_uuid", contrato_id=contrato_id)
        return None

    try:
        contrato = await contrato_service.obtener_contrato(db, usuario.id, parsed_id)
    except Exception as exc:
        # Broad by design: `obtener_contrato` raises `NotFoundError` for an unknown or
        # not-owned contrato_id, but this helper must never let ANY failure here (a bad
        # id is just an untrusted hint from the client) escalate into a 4xx/5xx for the
        # whole chat request.
        await logger.adebug("agent_chat_contrato_context_not_resolved", contrato_id=contrato_id, error=str(exc))
        return None

    objeto = contrato.objeto[:_MAX_OBJETO_CONTEXT_CHARS] if contrato.objeto else ""
    return (
        "El usuario está trabajando sobre este contrato (usa este contrato_id cuando "
        "necesites un contrato; NO lo pidas): "
        f"contrato_id={contrato.id} | N° contrato: {contrato.numero_contrato} | "
        f"Entidad: {contrato.entidad or 'no registrada'} | Objeto: {objeto}."
    )


def _extract_attachment_text(attachment: ToolAttachment) -> str:
    """Best-effort text extraction for the system-prompt preview. Never raises."""
    try:
        text = parse_document(attachment.data, attachment.filename)
    except Exception as exc:
        logger.warning("agent_chat_attachment_parse_failed", filename=attachment.filename, error=str(exc))
        return ""
    return text or ""


def _expand_attachments_for_tools(attachments: dict[str, ToolAttachment]) -> dict[str, ToolAttachment]:
    """Return a NEW dict = `attachments` plus, for every archive attachment, one
    entry per importable member — so `importar_documento` can resolve a file that
    lives INSIDE a dropped `.zip`/`.tar`/`.tar.gz`/`.tgz`, not just the archive
    itself.

    Executable/image/media members (`document_parser._NON_TEXT_EXTENSIONS`) are
    never exposed — everything else (rich docs like .pdf/.docx/.xlsx/.xls, and
    text-like formats) is. Members are keyed by their basename; on a key collision
    (with an original attachment or another archive's member), the key falls back
    to `f"{archive_name}:{member_path}"`. Bounded by `_MAX_EXPANDED_ATTACHMENTS`
    total expanded members across all archives in this turn, on top of
    `iter_archive_members`'s own per-archive member cap.
    """
    expanded: dict[str, ToolAttachment] = dict(attachments)
    added = 0

    for archive_name, attachment in attachments.items():
        if added >= _MAX_EXPANDED_ATTACHMENTS:
            break
        if not is_archive_filename(archive_name):
            continue
        try:
            for member_path, member_data in iter_archive_members(attachment.data, archive_name):
                if added >= _MAX_EXPANDED_ATTACHMENTS:
                    break
                ext = Path(member_path).suffix.lower()
                if ext in _NON_TEXT_EXTENSIONS:
                    continue

                key = Path(member_path).name
                if key in expanded:
                    key = f"{archive_name}:{member_path}"

                content_type, _ = mimetypes.guess_type(member_path)
                expanded[key] = ToolAttachment(
                    filename=key,
                    content_type=content_type or "application/octet-stream",
                    data=member_data,
                )
                added += 1
        except Exception as exc:
            logger.warning("agent_chat_archive_expand_failed", filename=archive_name, error=str(exc))
            continue

    return expanded


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse `text` as JSON, returning it only if the top-level value is an object."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_json_candidates(content: str) -> list[dict[str, Any]]:
    """Best-effort extraction of candidate JSON objects from raw model `content`.

    Tries, in order, until one strategy yields at least one object: (a) the whole
    trimmed content as a single JSON object, (b) every ```json/``` fenced code
    block, (c) the first balanced `{...}` substring found anywhere in the text.
    Never raises — a strategy that finds nothing just falls through to the next.
    """
    stripped = content.strip()
    if not stripped:
        return []

    whole = _try_parse_json_object(stripped)
    if whole is not None:
        return [whole]

    fenced_candidates = [
        parsed
        for block in _FENCED_BLOCK_RE.findall(content)
        if (parsed := _try_parse_json_object(block.strip())) is not None
    ]
    if fenced_candidates:
        return fenced_candidates

    for brace_block in _iter_balanced_braces(content):
        parsed = _try_parse_json_object(brace_block)
        if parsed is not None:
            return [parsed]

    return []


def _iter_balanced_braces(text: str) -> list[str]:
    """Yield top-level `{...}` substrings of `text`, honoring quoted strings so a
    `}` inside a string value doesn't prematurely close the object.
    """
    blocks: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        j = i
        while j < n:
            ch = text[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[i : j + 1])
                    break
            j += 1
        i = j + 1
    return blocks


def _coerce_args_dict(value: Any) -> dict[str, Any] | None:
    """Best-effort coercion of a tool-call "arguments" value into a dict.

    Accepts either an already-parsed dict, or a JSON-encoded string (some models —
    and litellm's own OpenAI-shaped tool_calls — represent `arguments` as a raw
    JSON string rather than a nested object).
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


_TOOL_CALL_WRAPPER_KEYS = {"function", "name", "tool", "arguments", "parameters", "args"}


def _normalize_tool_args(args: Any) -> dict[str, Any]:
    """Unwrap a tool-call arguments dict a weak model wrapped in an OpenAI-ish
    envelope before it reaches `invoke_tool`.

    llama3.1:8b sometimes emits the arguments as `{"function": "crear_cuenta_cobro",
    "parameters": {...}}` or `{"name": ..., "arguments": {...}}` instead of the real
    arguments — passing that straight to the tool's input model would always fail
    validation. When EVERY key of `args` is an envelope key (so a genuine argument
    named, say, `contrato_id` is never stripped), return the inner params dict; a
    wrapper with no inner object collapses to `{}`. Non-wrapper dicts pass through
    unchanged.
    """
    if not isinstance(args, dict) or not args:
        return args if isinstance(args, dict) else {}
    if not set(args.keys()) <= _TOOL_CALL_WRAPPER_KEYS:
        return args
    for inner_key in ("parameters", "arguments", "args"):
        inner = args.get(inner_key)
        if isinstance(inner, dict):
            return inner
    return {}


def _match_named_shape(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Match `{"name"/"tool"/"function": <tool name>, "arguments"/"parameters"/"args": {...}}`.

    Also accepts the OpenAI nested shape `{"function": {"name": ..., "arguments": ...}}`.
    Returns None (never raises) when no known tool name is present.
    """
    function_field = candidate.get("function")
    if isinstance(function_field, dict):
        nested_name = function_field.get("name")
        if isinstance(nested_name, str) and nested_name in TOOL_REGISTRY:
            args = _coerce_args_dict(function_field.get("arguments"))
            return nested_name, args if args is not None else {}

    name: str | None = None
    for key in ("name", "tool", "function"):
        value = candidate.get(key)
        if isinstance(value, str) and value in TOOL_REGISTRY:
            name = value
            break
    if name is None:
        return None

    for args_key in ("arguments", "parameters", "args"):
        args = _coerce_args_dict(candidate.get(args_key))
        if args is not None:
            return name, args
    return name, {}


def _match_bare_args_shape(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Match a dict that IS the arguments themselves (no name/tool key) to exactly
    one registered tool by field names: a tool qualifies if every key in `candidate`
    is a field of its `input_model` AND every required field of that model is
    present in `candidate`. Ambiguous (zero or 2+ qualifying tools) → None; this
    never guesses.
    """
    keys = set(candidate.keys())
    matches: list[str] = []
    for name, spec in TOOL_REGISTRY.items():
        fields = spec.input_model.model_fields
        if not keys <= set(fields.keys()):
            continue
        required = {field_name for field_name, info in fields.items() if info.is_required()}
        if not required <= keys:
            continue
        matches.append(name)

    if len(matches) == 1:
        return matches[0], candidate
    return None


def _recover_tool_calls_from_content(content: str) -> list[LLMToolCall]:
    """Recover tool call(s) a weak local model "drew" as plain text `content`
    instead of emitting a real function call (a known llama3.1:8b weakness — the
    model replies with e.g. `{"filename": "contrato.docx", ...}` as the message
    body and leaves `tool_calls` empty).

    Returns `[]` when nothing can be confidently recovered. Never raises.
    """
    if not content or not content.strip():
        return []

    try:
        candidates = _extract_json_candidates(content)
    except Exception:
        return []

    recovered: list[LLMToolCall] = []
    for candidate in candidates:
        try:
            match = _match_named_shape(candidate) or _match_bare_args_shape(candidate)
        except Exception:
            match = None
        if match is None:
            continue
        name, arguments = match
        recovered.append(LLMToolCall(id=f"recovered_{len(recovered)}", name=name, arguments=arguments))

    return recovered


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


# Maps an id-shaped field name to the read-only tool that discovers a real value for
# it — used by `_format_tool_error` to tell the LLM how to recover from a pydantic
# validation failure instead of just repeating the same bad call.
_ID_DISCOVERY_TOOLS = {
    "contrato_id": "listar_contratos",
    "cuenta_id": "listar_cuentas_cobro",
}


def _format_tool_error(exc: Exception, tool_name: str) -> tuple[str, str]:
    """Turn a tool-loop exception into `(user_resumen, llm_detail)`.

    `user_resumen` is what the USER sees in the trace (`ToolEvent.resumen`) — it must
    read as a clean Spanish sentence, never a raw pydantic/validation dump (the live
    bug this fixes: the UI showed "3 validation errors for CuentaCobroCreate ..."
    verbatim). `llm_detail` is fed back to the model as the tool result's "error"
    field — it can be more technical/actionable since only the LLM reads it, and its
    job is to make the model's NEXT tool call succeed instead of repeating the same
    mistake.
    """
    if isinstance(exc, ValidationError):
        campos = list(
            dict.fromkeys(".".join(str(part) for part in error.get("loc", ())) or "?" for error in exc.errors())
        )
        campos_text = ", ".join(campos)
        user_resumen = f"No pude ejecutar {tool_name}: faltan o son inválidos estos datos: {campos_text}."

        discovery_tools = [tool_hint for field_name, tool_hint in _ID_DISCOVERY_TOOLS.items() if field_name in campos]
        llm_parts = [f"No se pudo ejecutar `{tool_name}`: faltan o son inválidos estos argumentos: {campos_text}."]
        if discovery_tools:
            llm_parts.append(
                "Antes de reintentar, llama a "
                + " y a ".join(f"`{t}`" for t in discovery_tools)
                + " para obtener el/los identificador(es) real(es) — nunca inventes un UUID."
            )
        llm_parts.append(
            "Vuelve a llamar la herramienta incluyendo TODOS sus argumentos obligatorios "
            "(por ejemplo, mes como entero 1-12 y anio como entero), no solo el que faltó."
        )
        return user_resumen, " ".join(llm_parts)

    if isinstance(exc, DomainError):
        llm_detail = f"{exc.detail} (code={exc.code})" if exc.code else exc.detail
        return exc.detail, llm_detail

    return f"Ocurrió un error al ejecutar {tool_name}.", str(exc)[:300]


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
    contrato_id: str | None = None,
) -> AgentChatResult:
    """Run the free-form tool-calling loop for one user message and persist history.

    Tool-call/tool-result messages are exchanged with the LLM within this call only
    — they are never written to `Conversacion.mensajes_json`. Only the user message
    and the final assistant answer are persisted, mirroring `agent_service.chat`.

    `contrato_id` is an OPTIONAL contract context supplied by the caller (e.g. the
    contract the user currently has open in the UI) — see `_resolve_contrato_context`.
    It saves the agent a round trip of asking the user for a UUID they don't have.
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

    # Attachments passed to tools (unlike the preview blocks above, which always
    # describe the ORIGINAL uploads) are expanded so an archive's members are each
    # individually resolvable by `importar_documento` via `ctx.attachments[filename]`.
    expanded_attachments = _expand_attachments_for_tools(attachments)
    importable_filenames = list(expanded_attachments.keys()) if attachments else []

    contrato_context = await _resolve_contrato_context(db, usuario, contrato_id)
    system_prompt = _build_system_prompt(attachment_blocks, importable_filenames, contrato_context)
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=system_prompt),
        *history,
        LLMMessage(role="user", content=message),
    ]

    llm = get_llm()
    tools = to_openai_tools()
    tool_ctx = ToolContext(db=db, usuario=usuario, attachments=expanded_attachments)

    tool_events: list[ToolEvent] = []
    tokens_used = 0
    final_content = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await llm.complete(messages, tools=tools, temperature=0.2, max_tokens=1024)
        tokens_used += response.total_tokens

        calls = response.tool_calls
        recovered_from_content = False
        if not calls:
            # Weak local models (llama3.1:8b) sometimes "draw" the tool call as
            # plain text instead of a real function call — recover it here so the
            # loop still executes the tool instead of returning useless raw JSON.
            recovered = _recover_tool_calls_from_content(response.content or "")
            if recovered:
                calls = recovered
                recovered_from_content = True
                await logger.ainfo("agent_chat_recovered_tool_calls", count=len(recovered))

        if not calls:
            final_content = response.content
            messages.append(LLMMessage(role="assistant", content=final_content))
            break

        messages.append(
            LLMMessage(
                role="assistant",
                # When recovered, the original content WAS the tool call drawn as
                # text — echoing it back alongside the now-real tool_calls would
                # just confuse the model in the next turn, so it's dropped.
                content="" if recovered_from_content else (response.content or ""),
                tool_calls=[
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                    for call in calls
                ],
            )
        )

        for call in calls:
            spec = TOOL_REGISTRY.get(call.name)
            if spec is None:
                llm_detail = f"Unknown tool: {call.name}"
                user_resumen = f"No reconozco la herramienta solicitada ({call.name})."
                tool_events.append(ToolEvent(tool=call.name, status="error", resumen=user_resumen))
                result_payload: Any = {"error": llm_detail}
            else:
                try:
                    output = await invoke_tool(call.name, tool_ctx, _normalize_tool_args(call.arguments))
                    if "write" in spec.tags:
                        await db.commit()
                    dumped = output.model_dump(mode="json")
                    result_payload = dumped
                    tool_events.append(ToolEvent(tool=call.name, status="ok", resumen=_summarize_tool_result(dumped)))
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
                    user_resumen, llm_detail = _format_tool_error(exc, call.name)
                    result_payload = {"error": llm_detail}
                    tool_events.append(ToolEvent(tool=call.name, status="error", resumen=user_resumen))
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
