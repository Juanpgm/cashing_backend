"""Deterministic fake LLM adapter for local dev / E2E runs without a real model.

Unlike `LiteLLMAdapter`, `FakeLLMPort` never imports `litellm` and never makes a
network call. It implements the same surface the rest of the app calls through
`LLMPort` (`complete` / `stream` / `embed`) so the RUNNING APP — not just pytest,
which already has its own test-only seam (`tests/conftest.py::bloquear_red_llm` +
the `ScriptedLLM` pattern in `tests/test_agent_chat_service_iterations.py`) — can
complete a full tool-calling chat turn. Swapped in via `LLM_PROVIDER=fake` (see
`app.core.config.Settings` and `app.adapters.llm.litellm_adapter.get_llm`).
Intended for local dev without API keys and for future Playwright E2E runs
where the whole app boots for real.

Determinism, precisely stated (do not assume more than this): the SEQUENCE of
tool NAMES produced across a chat turn is deterministic — it is a pure
function of `HAPPY_PATH_SEQUENCE`, the last tool called in `messages`, and
(since slice 0.6) whether that last call's paired result SUCCEEDED or FAILED
(see `_resolve_next_tool` — a failure retries the same tool once, then gives
up, instead of blindly advancing). The synthesized argument VALUES inside each
tool call are mostly NOT deterministic — `synthesize_tool_arguments` calls
`uuid.uuid4()` for any UUID field NOT already present in `known` (see
`_known_ids`, slice 0.6 — real ids threaded from earlier tool results in the
SAME `messages` history ARE deterministic and reused) and
`datetime.date.today()` for date fields, so two runs of the identical scripted
sequence still produce different non-id argument payloads (and, against a real
DB, different domain outcomes for those fields — see
`tests/test_fake_llm_adapter.py::test_fake_llm_completes_the_full_tool_sequence_even_when_synthesized_ids_dont_resolve`).

Design: `complete()` is a PURE function of `messages` PLUS the process-wide
`settings.FAKE_LLM_SCRIPT` knob (slice 0.6 — see that method's own docstring
for why this one exception is safe) — no INSTANCE/mutable module state, so two
concurrent chat sessions sharing (or each holding their own) `FakeLLMPort`
never leak script position OR known ids into each other.
"""

from __future__ import annotations

import datetime
import enum
import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, get_args, get_origin

import structlog
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.schemas.agent import AGENT_RECAP_MARKER, LLMMessage, LLMResponse, LLMToolCall
from app.tools.registry import TOOL_REGISTRY

logger = structlog.get_logger("llm.fake")

# --- Happy-path script --------------------------------------------------------
#
# Keyed by the name of the last tool the agent loop actually invoked (`None` = no
# tool called yet — the first turn). Each entry names the NEXT tool to call, or
# `None` to end the chain with a plain text reply. Extend this dict (not the
# routing logic in `FakeLLMPort.complete`) to script more of the radicación chain
# in a future slice — e.g. add "radicar_cuenta": "generar_paquete_evidencias".
HAPPY_PATH_SEQUENCE: dict[str | None, str | None] = {
    None: "listar_contratos",
    "listar_contratos": "crear_cuenta_cobro",
    "crear_cuenta_cobro": "definir_requisitos_checklist",
    "definir_requisitos_checklist": "importar_documento",
    "importar_documento": "resumen_checklist",
    "resumen_checklist": "radicar_cuenta",
    "radicar_cuenta": None,
}

_DONE_TEXT_RESPONSE = "Listo, terminé la cadena de radicación."
_DEFAULT_TEXT_RESPONSE = "Listo — no hay más pasos programados en el guion determinístico (FakeLLMPort)."
# `FAKE_LLM_SCRIPT=stall` — always returned instead of ANY tool call (see
# `FakeLLMPort.complete`'s early return), regardless of history.
_STALL_TEXT_RESPONSE = "Me detuve acá — no sigo con el siguiente paso (FAKE_LLM_SCRIPT=stall)."
# Outcome-aware routing: a scripted tool failed twice in a row (the retry also
# failed) — give up instead of looping forever.
_GAVE_UP_TEXT_RESPONSE = "No pude completar ese paso tras reintentarlo — me detengo acá."
# FAKE_LLM_SCRIPT=malformed corrupts exactly this ONE tool call in the sequence
# (see the "malformed" branch in `FakeLLMPort.complete`) — chosen because
# `crear_cuenta_cobro` has multiple required fields and a real, well-defined
# `ValidationError` -> `_format_tool_error` path in `agent_chat_service`.
_MALFORMED_TARGET_TOOL = "crear_cuenta_cobro"


def _last_tool_called(messages: list[LLMMessage]) -> str | None:
    """Name of the tool the most recent assistant tool_calls message requested,
    or `None` if no tool has been called yet in this history.

    Only looks at the FIRST tool call of that message: `FakeLLMPort` itself only
    ever emits one tool call per turn, so a message with several can only come
    from a different LLM/test fixture appended earlier in the same history —
    routing off the first one is a reasonable, simple default for that case too.
    """
    for message in reversed(messages):
        if message.role == "assistant" and message.tool_calls:
            fn = message.tool_calls[0].get("function") or {}
            name = fn.get("name")
            if isinstance(name, str):
                return name
    return None


def _safe_json_dict(content: Any) -> dict[str, Any] | None:
    """`json.loads(content)` guarded: any parse failure, or a value that parses to
    something other than a dict (e.g. a bare JSON list/number), returns `None`
    instead of raising — a tool result's content is always attacker/model-adjacent
    text as far as this adapter is concerned, never trusted to be well-formed."""
    if not isinstance(content, str):
        return None
    try:
        loaded = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _extract_ids_from_compact_listar_contratos(content: str) -> dict[str, str]:
    """`listar_contratos`'s REAL tool-result content in the running app is NOT
    JSON: `agent_chat_service._compact_listar_contratos` serializes it as plain
    `numero_contrato | entidad | valor_mensual | id` lines (one per contrato,
    most-recent-first, matching `listar_contratos`'s own ordering) — bypassing
    the generic `json.dumps(model_dump())` path entirely for this one tool, to
    keep the full list readable for the model without JSON overhead. Parses the
    LAST pipe-separated token of the FIRST line as `contrato_id` — guarded: any
    shape mismatch (no contratos, no pipes, a malformed line) yields `{}`,
    never raises."""
    first_line = content.strip().splitlines()[0] if content.strip() else ""
    if "|" not in first_line:
        return {}
    candidate = first_line.rsplit("|", 1)[-1].strip()
    return {"id": candidate} if _is_uuid_shaped(candidate) else {}


def _parse_tool_result_content(tool_name: str, content: Any) -> dict[str, Any] | None:
    """`json.loads(content)` guarded, PLUS a tool-specific fallback for
    `listar_contratos`'s non-JSON compact real-app serialization (see
    `_extract_ids_from_compact_listar_contratos`) — every other tool's content
    is plain JSON (`_serialize_tool_result`'s generic path) and only needs the
    guarded `json.loads`."""
    parsed = _safe_json_dict(content)
    if parsed is not None:
        return parsed
    if tool_name == "listar_contratos" and isinstance(content, str):
        compact_ids = _extract_ids_from_compact_listar_contratos(content)
        if compact_ids:
            return compact_ids
    # Genuinely unparseable (or listar_contratos's own compact fallback found
    # no ids either) — fail open (never raise), but make the degrade
    # observable: this is the path a `_MAX_TOOL_RESULT_CHARS` truncation
    # (`agent_chat_service._serialize_tool_result`) silently hits, breaking
    # `json.loads` mid-structure with no other signal that ids stopped
    # threading through `_known_ids`.
    logger.warning("fake_llm_tool_result_unparseable", tool=tool_name)
    return None


def _tool_results(messages: list[LLMMessage]) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Pair each assistant `tool_calls[0]` `(id, name)` with the `role="tool"`
    message carrying a matching `tool_call_id` LATER in `messages` — mirrors how
    `agent_chat_service.chat_with_tools` actually appends history (one assistant
    tool_calls message immediately followed by its `role="tool"` result, repeated
    per iteration). An assistant call with no later matching tool message yet
    (still pending) is simply NOT included — there is nothing to judge its outcome
    from yet, see `_last_result_for`.

    Only looks at `tool_calls[0]` per assistant message, same single-call
    convention as `_last_tool_called` (this fake only ever emits one).
    """
    pending: dict[str, str] = {}
    results: list[tuple[str, str, dict[str, Any] | None]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            fn = message.tool_calls[0].get("function") or {}
            name = fn.get("name")
            call_id = message.tool_calls[0].get("id")
            if isinstance(name, str) and isinstance(call_id, str):
                pending[call_id] = name
        elif message.role == "tool" and message.tool_call_id in pending:
            call_id = message.tool_call_id
            name = pending.pop(call_id)
            results.append((call_id, name, _parse_tool_result_content(name, message.content)))
    return results


def _is_uuid_shaped(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


# Maps a producing tool's name -> {raw output field name: field name the NEXT tool
# in HAPPY_PATH_SEQUENCE actually expects}. Derived from the REAL Pydantic models,
# not guessed:
#   - listar_contratos  -> ListarContratosOutput.contratos: list[ContratoResumen],
#     each with `id` (app/tools/catalog/listar_contratos.py) -> the next tool,
#     crear_cuenta_cobro, needs `contrato_id` (CrearCuentaCobroInput).
#   - crear_cuenta_cobro -> CuentaCobroResponse.id (app/schemas/cuenta_cobro.py,
#     the cuenta itself) -> every downstream tool that needs the cuenta needs
#     `cuenta_id` (DefinirRequisitosChecklistInput, ResumenChecklistInput,
#     RadicarCuentaInput). `CuentaCobroResponse.contrato_id` already matches its
#     own field name, no alias needed for that one (generic passthrough covers it).
#   - definir_requisitos_checklist -> DefinirRequisitosChecklistOutput.
#     cuenta_cobro_id (app/tools/catalog/requisitos.py) -> `cuenta_id`.
#   - resumen_checklist -> ChecklistResponse.cuenta_cobro_id
#     (app/schemas/checklist.py, same field name as above) -> `cuenta_id`. NOTE:
#     this alias is a documented NO-OP on the LIVE, same-turn path (`_known_ids`
#     via `_tool_results`/`_parse_tool_result_content`) — the real app never
#     serializes `resumen_checklist`'s tool-result content as JSON at all, it
#     goes through `agent_chat_service._compact_resumen_checklist`'s plain-text
#     summary (`"resumen: total=... / {codigo} {estado}"` lines, no id field),
#     which `_parse_tool_result_content` correctly can't parse and returns
#     `None` for (see `test_known_ids_resumen_checklist_real_compact_text_yields_no_cuenta_id`).
#     It DOES matter on the cross-turn recap-resume path (`_resume_from_recap`),
#     which reads the RAW dumped dict `agent_chat_service._build_tool_context_recap`
#     built the recap from, not this serialized text — see
#     `test_complete_resumes_from_recap_threads_cuenta_id_via_resumen_checklist_alias`.
#     Kept here (rather than removed) for that reason, and because `cuenta_id` is
#     harmlessly already known by this point in the live happy path anyway (from
#     `crear_cuenta_cobro`'s own alias, earlier in the same `messages` history).
_ID_ALIASES: dict[str, dict[str, str]] = {
    "listar_contratos": {"id": "contrato_id"},
    "crear_cuenta_cobro": {"id": "cuenta_id"},
    "definir_requisitos_checklist": {"cuenta_cobro_id": "cuenta_id"},
    "resumen_checklist": {"cuenta_cobro_id": "cuenta_id"},
}


def _extract_id_fields(result: dict[str, Any]) -> dict[str, str]:
    """Collect UUID-shaped `id`/`*_id` string values out of a tool's dumped JSON
    result: top-level fields, PLUS one level into the first item of any top-level
    list value (covers `ListarContratosOutput.contratos[0].id` — the only nested
    shape the Phase 0 happy-path script's tools actually produce; deeper nesting
    is out of scope for this slice). Same id/`*_id` + UUID-validate convention as
    `agent_chat_service._extract_recap_ids` (same purpose, a dumped tool result) —
    duplicated locally rather than imported: an adapter importing a service would
    invert this codebase's hexagonal dependency direction (services depend on
    adapters via ports, never the reverse — see CLAUDE.md's Anti-Patterns)."""
    found: dict[str, str] = {}
    for key, value in result.items():
        if key == "id" or key.endswith("_id"):
            if _is_uuid_shaped(value):
                found[key] = value
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            for nested_key, nested_value in value[0].items():
                if (nested_key == "id" or nested_key.endswith("_id")) and _is_uuid_shaped(nested_value):
                    found.setdefault(nested_key, nested_value)
    return found


def _known_ids(messages: list[LLMMessage]) -> dict[str, str]:
    """Flat `{field_name: uuid_string}` pool built from every RESOLVED tool result
    in `messages`, in call order (a later call's id for the same field name wins —
    it's the freshest). Each raw id is registered under its OWN field name
    (generic passthrough — e.g. `crear_cuenta_cobro`'s echoed `contrato_id`
    already matches what `importar_documento` would want) AND, when the producing
    tool has an entry in `_ID_ALIASES`, additionally under the alias target the
    NEXT tool in the happy path actually expects."""
    known: dict[str, str] = {}
    for _call_id, tool_name, result in _tool_results(messages):
        if not isinstance(result, dict):
            continue
        raw_ids = _extract_id_fields(result)
        if not raw_ids:
            continue
        known.update(raw_ids)
        for raw_key, target_key in _ID_ALIASES.get(tool_name, {}).items():
            if raw_key in raw_ids:
                known[target_key] = raw_ids[raw_key]
    return known


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` -> `X`. Returns `annotation` unchanged for any other shape."""
    origin = get_origin(annotation)
    if origin is not None and type(None) in get_args(annotation):
        remaining = [a for a in get_args(annotation) if a is not type(None)]
        if remaining:
            return remaining[0]
    return annotation


def _numeric_bound(field_info: Any, attr: str) -> int | float | None:
    """Read a `Ge`/`Gt`/`Le`/`Lt` (annotated_types) constraint off a Pydantic field,
    if the tool's input model declared one (e.g. `mes: int = Field(ge=1, le=12)`)."""
    for meta in getattr(field_info, "metadata", []):
        value = getattr(meta, attr, None)
        if isinstance(value, int | float):
            return value
    return None


def _synthesize_field_value(field_info: Any) -> Any:
    """Produce one schema-valid dummy value for a REQUIRED Pydantic field.

    Covers every required-field shape actually used across `app.tools.catalog`'s
    32 tools today: `uuid.UUID`, `str`, `int` (honoring `ge`/`gt` bounds), `date`,
    and `StrEnum`/`Literal`. Anything else falls back to `None` and lets the
    caller's `model_validate` raise — a loud, specific failure (naming the tool
    and field) beats silently emitting an invalid tool call for a shape this
    synthesizer doesn't know about yet.

    Values are always JSON-primitive (str/int/float/bool/list/dict/None) rather
    than the native Python type (`str(uuid4())` not `uuid4()`, an ISO date string
    not a `date`) — exactly like a REAL provider's tool-call arguments, which
    arrive over the wire as JSON and get `json.loads`'d back into a plain dict
    (see `LiteLLMAdapter._parse_tool_calls`). `agent_chat_service.chat_with_tools`
    later does `json.dumps(call.arguments)` when replaying the assistant message
    into history — a native `uuid.UUID`/`Decimal`/`date` there would raise
    `TypeError: Object of type X is not JSON serializable`. Pydantic still
    coerces these plain strings back into the real type on `model_validate`.
    """
    annotation = _unwrap_optional(field_info.annotation)
    origin = get_origin(annotation)

    if origin is not None:
        args = get_args(annotation)
        if origin is list:
            return []
        if origin is dict:
            return {}
        # typing.Literal[...] — Python's typing module has no importable "Literal"
        # origin sentinel to compare against directly; string-matching its repr is
        # the standard workaround.
        if str(origin) == "typing.Literal" and args:
            return args[0]

    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation)).value
        if annotation is uuid.UUID:
            return str(uuid.uuid4())
        if annotation is bool:
            return False
        if annotation is int:
            lo = _numeric_bound(field_info, "ge")
            if lo is None:
                gt = _numeric_bound(field_info, "gt")
                lo = gt + 1 if gt is not None else None
            return lo if lo is not None else 1
        if annotation is float:
            return 1.0
        if annotation is Decimal:
            return "1"
        if annotation is datetime.date:
            return datetime.date.today().isoformat()
        if annotation is datetime.datetime:
            return datetime.datetime.now(datetime.UTC).isoformat()
        if annotation is str:
            return "fake"

    return None


def synthesize_tool_arguments(input_model: type[BaseModel], known: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a minimal, schema-VALID argument dict for `input_model`.

    Fills only the fields Pydantic requires (no default/default_factory) and
    leaves every optional field to its own default — mirrors how a real model
    would call a tool when the user hasn't specified optional knobs.

    `known`: optional `{field_name: uuid_string}` pool (see `_known_ids`) of REAL
    ids threaded from earlier tool results in this same chat turn/session — when a
    REQUIRED field's name is a key in `known` AND the field is UUID-shaped, its
    real value is used instead of a fresh random `uuid.uuid4()`. Every other
    synthesis rule (dates, enums, numeric bounds, non-UUID/non-matching fields)
    is unchanged. `known=None` (the default) reproduces the exact pre-existing
    behavior — always a fresh random value for every UUID field.
    """
    known = known or {}
    values: dict[str, Any] = {}
    for name, field_info in input_model.model_fields.items():
        if not field_info.is_required():
            continue
        if name in known and _unwrap_optional(field_info.annotation) is uuid.UUID:
            values[name] = known[name]
            continue
        values[name] = _synthesize_field_value(field_info)
    return values


def build_tool_call(
    tool_name: str, input_model: type[BaseModel], call_id: str, known: dict[str, str] | None = None
) -> LLMToolCall:
    """Build a schema-valid `LLMToolCall` for `tool_name`, validated against its
    own `input_model` before being handed back (fails loudly, at fake-adapter
    build time, if a future catalog tool needs a shape this synthesizer can't
    fill — never silently emits a call the real tool invoker would reject).

    See `synthesize_tool_arguments` for `known`.
    """
    arguments = synthesize_tool_arguments(input_model, known=known)
    input_model.model_validate(arguments)
    return LLMToolCall(id=call_id, name=tool_name, arguments=arguments)


def _is_error_result(result: dict[str, Any] | None) -> bool:
    """Whether a paired tool-result payload represents a FAILED call.

    Matches the REAL shape `agent_chat_service.chat_with_tools` actually
    serializes for a failed tool call: `{"error": <str>}` (see
    `_format_tool_error` + `result_payload = {"error": llm_detail}` in that
    module). `"detail"` is accepted too as a defensive extra for hand-built
    test histories using that common FastAPI convention — the real app never
    produces it at this call site, only `"error"`.

    A `None` result (no paired tool message yet, or its content wasn't valid
    JSON) is treated as NOT a failure — there is no evidence either way, and
    defaulting to "keep going" matches this file's existing safe-degrade
    philosophy over retry-looping on content it can't even parse.
    """
    if result is None:
        return False
    return "error" in result or "detail" in result


def _find_recap_content(messages: list[LLMMessage]) -> str | None:
    """The most recent `role="system"` message whose content starts with
    `AGENT_RECAP_MARKER` (`agent_chat_service._build_tool_context_recap`'s exact
    prefix), or `None`. `agent_service.get_conversation_history` only ever keeps
    ONE such message at a time (refreshed, not accumulated — see that module's
    docstring), but scanning for the LAST match is a harmless, defensive choice
    if a hand-built history ever carried more than one."""
    recap: str | None = None
    for message in messages:
        if (
            message.role == "system"
            and isinstance(message.content, str)
            and message.content.startswith(AGENT_RECAP_MARKER)
        ):
            recap = message.content
    return recap


def _parse_recap_entry(entry: str) -> tuple[str, str, dict[str, str]] | None:
    """Parse ONE `tool_name:status[ k=v ...]` recap entry (see
    `agent_chat_service._build_tool_context_recap`'s exact line format) into
    `(tool_name, status, ids)`. Returns `None` for anything that doesn't match
    that shape — never raises, the caller treats that as "skip this entry"."""
    entry = entry.strip()
    if not entry or ":" not in entry:
        return None
    tool_name, _, rest = entry.partition(":")
    tool_name = tool_name.strip()
    if not tool_name:
        return None
    status, _, id_part = rest.strip().partition(" ")
    status = status.strip()
    if not status:
        return None
    ids: dict[str, str] = {}
    for token in id_part.split():
        key, sep, value = token.partition("=")
        if sep and key and _is_uuid_shaped(value):
            ids[key] = value
    return tool_name, status, ids


def _resume_from_recap(messages: list[LLMMessage]) -> tuple[str | None, dict[str, str]]:
    """When `messages` carries no assistant `tool_calls` at all (a continuation
    turn replaying ONLY the persisted user/assistant history — see
    `agent_chat_service._build_tool_context_recap`'s docstring), derive:
      1. which tool to resume `HAPPY_PATH_SEQUENCE` from — the NEWEST `ok` entry
         in the recap (recap lines are newest-first: `_build_tool_context_recap`
         builds from `reversed(call_results)`).
      2. the `known` ids pool to seed — merged from EVERY `ok` entry (not just
         the newest), aliased exactly like `_known_ids` aliases a live tool
         result, newest value wins per field name.

    Returns `(None, {})` for a missing/malformed/empty recap — the caller
    (`_resolve_next_tool`) then starts the sequence fresh via `_advance(None)`,
    identical to no history at all. Never raises.
    """
    recap = _find_recap_content(messages)
    if recap is None:
        return None, {}

    body = recap[len(AGENT_RECAP_MARKER) :].strip()
    if not body:
        return None, {}

    resume_tool: str | None = None
    known: dict[str, str] = {}
    for raw_entry in body.split(" | "):
        parsed = _parse_recap_entry(raw_entry)
        if parsed is None:
            continue
        tool_name, status, ids = parsed
        if status != "ok":
            continue
        if resume_tool is None:
            resume_tool = tool_name
        # Newest-first iteration: setdefault so an OLDER entry never overwrites
        # a field name a NEWER entry already supplied.
        for key, value in ids.items():
            known.setdefault(key, value)
        for raw_key, target_key in _ID_ALIASES.get(tool_name, {}).items():
            if raw_key in ids:
                known.setdefault(target_key, ids[raw_key])

    return resume_tool, known


def _advance(last_tool: str | None) -> tuple[str | None, str]:
    """Look up the next scripted tool for `last_tool` in `HAPPY_PATH_SEQUENCE`.

    Returns `(next_tool, reason)`. `reason` is only meaningful when `next_tool`
    is `None`: `"unscripted"` (last_tool isn't a HAPPY_PATH_SEQUENCE key at all)
    vs `"done"` (last_tool's next step is the terminal `None` — the chain
    finished normally). `last_tool=None` itself is a normal, scripted key (the
    "no tool called yet" / first-turn position), not unscripted.
    """
    if last_tool not in HAPPY_PATH_SEQUENCE:
        return None, "unscripted"
    next_tool = HAPPY_PATH_SEQUENCE[last_tool]
    return next_tool, ("call" if next_tool is not None else "done")


def _resolve_next_tool(messages: list[LLMMessage]) -> tuple[str | None, str, dict[str, str]]:
    """OUTCOME-aware routing decision for one `complete()` call.

    Returns `(next_tool_or_None, reason, known_ids)`:
    - `reason` is `"call"` when `next_tool` is set; otherwise one of `"done"`
      (chain finished), `"unscripted"` (last tool called isn't in the happy
      path), or `"gave_up"` (a scripted tool failed twice in a row — see
      below).
    - `known_ids` is the `{field_name: uuid_string}` pool to thread into
      `synthesize_tool_arguments` for whichever tool gets called next (see
      `_known_ids` / `_resume_from_recap`).

    A scripted step only counts as "successfully done" when its paired tool
    result did NOT fail (`_is_error_result`) — this is what makes routing
    outcome-aware instead of blindly trusting that a tool NAME being called
    means it succeeded (the exact gap flagged in slice 0.4's review). A failed
    call retries the SAME tool name once; a SECOND consecutive failure of that
    same tool gives up (plain text reply) instead of looping forever.

    `_last_tool_called` (name-only, no pairing required) is still used to find
    "the last tool called" — this keeps every pre-existing test/behavior built
    around it (e.g. an assistant `tool_calls` message with no paired result
    yet) working unchanged; `_tool_results`/`_is_error_result` only ADD the
    outcome check on top.
    """
    last_tool = _last_tool_called(messages)

    if last_tool is None:
        recap_tool, recap_known = _resume_from_recap(messages)
        next_tool, reason = _advance(recap_tool)
        return next_tool, reason, recap_known

    known = _known_ids(messages)

    if last_tool not in HAPPY_PATH_SEQUENCE:
        # Unscripted tool (not a key in HAPPY_PATH_SEQUENCE at all) — the
        # retry-once bookkeeping below only makes sense for a SCRIPTED tool
        # (whose next step is a real, known entry). Check membership before
        # even looking at the result's outcome, matching the non-error
        # "unscripted -> text" behavior below instead of accidentally
        # entering the retry branch first.
        return None, "unscripted", known

    tool_results = _tool_results(messages)
    last_result: dict[str, Any] | None = None
    for _call_id, name, result in reversed(tool_results):
        if name == last_tool:
            last_result = result
            break

    if not _is_error_result(last_result):
        next_tool, reason = _advance(last_tool)
        return next_tool, reason, known

    trailing_failures = 0
    for _call_id, name, result in reversed(tool_results):
        if name == last_tool and _is_error_result(result):
            trailing_failures += 1
        else:
            break
    if trailing_failures >= 2:
        return None, "gave_up", known
    return last_tool, "call", known


class FakeLLMPort:
    """Deterministic `LLMPort` implementation. No network, no `litellm` import."""

    def __init__(self, default_model: str | None = None) -> None:
        self._default_model = default_model or "fake/deterministic"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        fallback: bool = True,
    ) -> LLMResponse:
        """Route to the next scripted tool call, or a plain text reply.

        Never raises for an unrecognized message shape or an unscripted routing
        position — those degrade to a safe plain-text `LLMResponse` (see
        `_DEFAULT_TEXT_RESPONSE`), exactly like the real provider chain
        eventually would after exhausting its fallbacks, so callers built around
        `LiteLLMAdapter`'s contract don't need special-casing for the fake.

        Reads the process-wide `settings.FAKE_LLM_SCRIPT` config knob (see
        `app.core.config.Settings`) to select "stall"/"malformed" behavior — the
        one piece of state this otherwise pure-function-of-`messages` adapter
        depends on. It's a deliberate, documented exception: it's a dev/test
        SCRIPT SELECTION knob (like `LLM_PROVIDER` itself, which already governs
        whether this class is even instantiated), not per-session mutable state —
        two calls with the same `messages` AND the same `FAKE_LLM_SCRIPT` value
        still always agree, which is all `test_concurrent_calls_do_not_leak_state_between_sessions`
        (unset/happy throughout) actually needs.
        """
        del temperature, max_tokens, tool_choice, reasoning_effort, fallback
        used_model = model or self._default_model
        script = settings.FAKE_LLM_SCRIPT

        if script == "stall":
            # Always stall, regardless of history — exercises "the agent gave up
            # mid-chain": one turn, no tool call, no progress, no crash.
            return LLMResponse(content=_STALL_TEXT_RESPONSE, model=used_model)

        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            try:
                known_for_structured = _resolve_next_tool(messages)[2]
                structured = synthesize_tool_arguments(response_format, known=known_for_structured)
                response_format.model_validate(structured)
            except Exception:
                await logger.awarning("fake_llm_structured_output_synthesis_failed", model=response_format.__name__)
                return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)
            return LLMResponse(content=json.dumps(structured), model=used_model)

        try:
            next_tool, reason, known = _resolve_next_tool(messages)
        except Exception:
            await logger.awarning("fake_llm_malformed_history")
            return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)

        if next_tool is None:
            text = {"done": _DONE_TEXT_RESPONSE, "gave_up": _GAVE_UP_TEXT_RESPONSE}.get(reason, _DEFAULT_TEXT_RESPONSE)
            return LLMResponse(content=text, model=used_model)

        if tools is not None:
            offered: set[str | None] = set()
            for t in tools:
                function = t.get("function")
                # A hand-built or third-party `tools` entry could have a
                # non-dict `function` value (mirrors the malformed
                # `tool_calls[*].function` shape already guarded for below) —
                # skip it instead of an `AttributeError` on `.get("name")`.
                if isinstance(function, dict):
                    offered.add(function.get("name"))
            if next_tool not in offered:
                # The caller didn't advertise the scripted tool this turn — never
                # request a tool call the caller can't actually dispatch.
                return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)

        spec = TOOL_REGISTRY.get(next_tool)
        if spec is None:
            # Catalog not imported yet (`app.tools.catalog` import-for-side-effect
            # missing from this call path) — degrade instead of KeyError-ing.
            await logger.awarning("fake_llm_tool_not_registered", tool=next_tool)
            return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)

        call_id = f"fake_call_{len(messages)}_{next_tool}"

        if script == "malformed" and next_tool == _MALFORMED_TARGET_TOOL:
            # Deliberately SKIP this adapter's own `build_tool_call` self-check
            # (`input_model.model_validate`) for this ONE scripted call — emit
            # arguments the tool's REAL `invoke_tool` validation will reject
            # (`mes=13` violates `CrearCuentaCobroInput.mes`'s `Field(ge=1,
            # le=12)`), so the `ValidationError` propagates all the way into
            # `agent_chat_service.chat_with_tools`'s per-tool-call handler and
            # its `_format_tool_error` — exercising the real
            # "malformed LLM response" path end-to-end, not just a fake-only
            # short-circuit. See tests exercising `FAKE_LLM_SCRIPT=malformed`.
            malformed_args = synthesize_tool_arguments(spec.input_model, known=known)
            malformed_args["mes"] = 13
            tool_call = LLMToolCall(id=call_id, name=next_tool, arguments=malformed_args)
            return LLMResponse(content="", model=used_model, tool_calls=[tool_call])

        try:
            tool_call = build_tool_call(next_tool, spec.input_model, call_id=call_id, known=known)
        except ValidationError:
            # The synthesizer produced a value the tool's own schema rejects
            # (e.g. a constraint shape `_synthesize_field_value` doesn't know
            # about yet) — degrade to a safe plain-text reply instead of
            # letting a `ValidationError` propagate up into `complete()`'s
            # caller, where it would be misdiagnosed as an LLM-network failure
            # rather than a fake-adapter synthesis gap.
            await logger.awarning("fake_llm_synthesized_arguments_invalid", tool=next_tool)
            return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)
        return LLMResponse(content="", model=used_model, tool_calls=[tool_call])

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Minimal fake stream — yields the non-streaming content in a single chunk."""
        response = await self.complete(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        yield response.content

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Deterministic fixed-size zero vector per input — embeddings from this
        adapter are never persisted (see `LLMPort.embed` docstring), so exact
        values don't matter, only that the shape (`len(texts)` vectors) is right."""
        del model
        return [[0.0] * 8 for _ in texts]


def get_fake_llm(model: str | None = None) -> FakeLLMPort:
    """Factory mirroring `litellm_adapter.get_llm`'s signature/shape."""
    return FakeLLMPort(default_model=model)
