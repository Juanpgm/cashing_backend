"""Conservative per-iteration tool-visibility gate for the free-form agent chat
loop (`app.services.agent_chat_service.chat_with_tools`).

Problem (radicacion-sin-friccion 1.8): every registered tool's full JSON schema
is exposed to the LLM on EVERY completion call regardless of where the
conversation actually is — SECOP/adiciones/plantilla tools stay in the prompt
even when nothing in the conversation could possibly need them yet, and every
cuenta-scoped tool (checklist, actividades, evidencias, informes, paquete,
radicar) is offered before a cuenta de cobro even exists, when calling ANY of
them would just fail (ownership lookup on a UUID nothing has produced yet) or
return an empty/`requisitos_definidos=false` shape.

Design: two independent, additive gates. Both are meant to be recomputed FRESH
before every single `llm.complete()` call (not once per turn) so a tool that
becomes relevant mid-turn — e.g. `definir_requisitos_checklist` right after
`crear_cuenta_cobro` succeeds in the SAME turn — is never missing from the very
next iteration's offered tools. This is required for correctness, not just an
optimization: the canonical playbook chains many tool calls autonomously
within ONE turn (see `agent_chat_service.SYSTEM_PROMPT_TEMPLATE`'s 10-step
order), and `FakeLLMPort.complete` — and, by construction, any real model
honoring the function list it was actually offered — will never call a tool
that isn't present in that specific completion call's `tools=[...]`.

1. CUENTA-SCOPED gate: a tool whose input schema REQUIRES `cuenta_id` or
   `cuenta_cobro_id` is hidden until a real cuenta id has been observed
   somewhere in this conversation — either from a cross-turn recap
   (`AGENT_RECAP_MARKER`, e.g. after `crear_cuenta_cobro` ran in an earlier
   turn) or from a successful tool call already made THIS turn. The set is
   computed dynamically from `TOOL_REGISTRY` (never a hand-maintained name
   list), so a newly added cuenta-scoped tool is gated automatically — the
   exact kind of drift `app.tools.catalog.importar_documento`'s own
   `_TIPO_A_REQUISITO_CODIGO` comment warns about elsewhere in this codebase.

2. ORTHOGONAL-TOPIC gate: a small, explicit allowlist of tools that are never
   part of the documented radicación playbook at all — organism PDF templates
   (`ingerir_plantilla_organismo`, `obtener_plantilla_organismo`) and contract
   value amendments (`listar_adiciones_contrato`, `registrar_adicion_contrato`).
   Neither pair appears in `SYSTEM_PROMPT_TEMPLATE`'s canonical 10-step order
   or in `FakeLLMPort.HAPPY_PATH_SEQUENCE`. They stay hidden unless the
   current user message mentions a matching keyword, or one of them already
   ran successfully this conversation (so a follow-up turn about the same
   topic keeps it visible without the user having to repeat the keyword).

Both gates default to VISIBLE on any doubt — an unparsable/absent recap, an
empty message, or a tool absent from either rule set is never hidden. This
keeps the gate safe by construction: it can only ever omit a tool that is
either (a) guaranteed to fail right now for lack of a cuenta id, or (b)
demonstrably outside the playbook and not yet hinted at by the user — never a
step the documented flow (or a happy-path test) actually needs next.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Any

from app.tools.registry import TOOL_REGISTRY

_CUENTA_ID_FIELDS = ("cuenta_id", "cuenta_cobro_id")

# Tool name -> keyword substrings (lowercase) whose presence in the current (or
# a few recent — see `hidden_tool_names`'s `recent_messages`) user message
# re-exposes it even though nothing else in the conversation has used it yet.
# See module docstring, gate 2. Widened per adversarial review (CRITICAL 3):
# the original set only matched "adici"/"plantilla"/"template", missing the
# real-world synonyms these tools' own docstrings already use — see
# `app.tools.catalog.adiciones` ("Adición/prórroga/otrosí") and
# `app.tools.catalog.plantillas_organismo`.
ORTHOGONAL_TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ingerir_plantilla_organismo": ("plantilla", "template", "formato de la entidad", "machote", "modelo"),
    "obtener_plantilla_organismo": ("plantilla", "template", "formato de la entidad", "machote", "modelo"),
    "listar_adiciones_contrato": (
        "adici",  # adición / adicion / adicionar / adicionó
        "otrosi",
        "otrosí",
        "prorroga",
        "prórroga",
        "amplia",  # ampliación / ampliar (del valor)
    ),
    "registrar_adicion_contrato": (
        "adici",
        "otrosi",
        "otrosí",
        "prorroga",
        "prórroga",
        "amplia",
    ),
}

# Field signature of a dumped `CuentaCobroResponse`/`CuentaCobroResumen` record
# (see `app.schemas.cuenta_cobro.CuentaCobroResponse` and
# `app.tools.catalog.listar_cuentas_cobro.CuentaCobroResumen`) — used by
# `find_cuenta_ids` to recognize a real cuenta record by SHAPE, regardless of
# which tool returned it or how deeply nested it is in the result.
_CUENTA_RECORD_SIGNATURE_FIELDS = frozenset({"contrato_id", "mes", "anio", "estado", "valor"})

# Matches a "cuenta_id=<uuid>" or "cuenta_cobro_id=<uuid>" fragment inside the
# cross-turn recap text (see `agent_chat_service._build_tool_context_recap`).
_CUENTA_ID_IN_TEXT_RE = re.compile(r"\bcuenta(?:_cobro)?_id=([0-9a-fA-F-]{36})\b")


def cuenta_scoped_tool_names() -> frozenset[str]:
    """Every registered tool whose input model REQUIRES a cuenta id field.

    Computed fresh from `TOOL_REGISTRY` on every call (a dict comprehension
    over <40 entries — negligible next to one LLM round trip) instead of
    cached at import time, so a test that registers extra tools into a
    scratch registry always sees accurate results.
    """
    return frozenset(
        name
        for name, spec in TOOL_REGISTRY.items()
        if any(
            field in spec.input_model.model_fields and spec.input_model.model_fields[field].is_required()
            for field in _CUENTA_ID_FIELDS
        )
    )


def _looks_like_cuenta_record(candidate: Any) -> bool:
    """Duck-type check: does this dumped dict look like a `CuentaCobro` record
    (`CuentaCobroResponse`/`CuentaCobroResumen`), regardless of which tool
    produced it or how deeply it is nested in the result? Computed from the
    known field shape (`_CUENTA_RECORD_SIGNATURE_FIELDS`), not a
    hand-maintained list of tool/model names, so a tool added later that
    echoes back a full cuenta record is picked up automatically."""
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("id"), str)
        and _CUENTA_RECORD_SIGNATURE_FIELDS.issubset(candidate.keys())
    )


def find_cuenta_ids(dumped: dict[str, Any] | None) -> list[str]:
    """Every real cuenta de cobro id found ANYWHERE in a tool's dumped JSON
    result — top-level (e.g. `crear_cuenta_cobro`'s own record) or nested in a
    list (e.g. `listar_cuentas_cobro`'s `cuentas` field) — regardless of which
    tool produced it.

    BLOCKER 1 (radicacion-sin-friccion phase-gating adversarial review):
    `listar_cuentas_cobro` is the documented discovery path for "an existing
    cuenta, new chat" (see `SYSTEM_PROMPT_TEMPLATE`), but it is neither
    `crear_cuenta_cobro` nor a cuenta-SCOPED tool (its input never requires a
    cuenta id) — the OLD tool-name-based unlock never fired for it, even
    after it returned a real cuenta id, permanently hiding every cuenta-scoped
    tool for the rest of the session. This function makes the unlock
    EVIDENCE-based instead: it recognizes a cuenta record by its field shape
    wherever it appears in the payload.

    Preserves first-encountered order and de-duplicates. An empty/missing
    `dumped`, or a `dumped` with no cuenta-shaped record anywhere (e.g. an
    empty `listar_cuentas_cobro` result), correctly yields `[]` — no evidence,
    no unlock.
    """
    if not dumped:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _walk(value: Any) -> None:
        if _looks_like_cuenta_record(value):
            candidate_id = value["id"]
            try:
                uuid.UUID(candidate_id)
            except (ValueError, AttributeError, TypeError):
                pass
            else:
                if candidate_id not in seen:
                    seen.add(candidate_id)
                    found.append(candidate_id)
        if isinstance(value, dict):
            for nested in value.values():
                _walk(nested)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(dumped)
    return found


def latest_cuenta_id_from_recap(recap_text: str | None) -> str | None:
    """Most recent real cuenta id still present in the recap text, if any.

    Used both to answer `cuenta_known_from_recap` with an evidence-based rule
    (not a tool-name allowlist) and to keep a "sticky" cuenta id alive across
    recap rebuilds (see `agent_chat_service._build_tool_context_recap`'s
    `sticky_cuenta_id` param — BLOCKER 2 of the same review). The recap is
    built newest-first, so the FIRST regex match in the raw text is the most
    recent piece of evidence.
    """
    if not recap_text:
        return None
    match = _CUENTA_ID_IN_TEXT_RE.search(recap_text)
    return match.group(1) if match else None


def cuenta_known_from_recap(recap_text: str | None) -> bool:
    """Whether the cross-turn recap gives high-confidence evidence that a real
    cuenta de cobro already exists in this conversation.

    Primary signal (evidence-based, BLOCKER 1/2): a literal `cuenta_id=` or
    `cuenta_cobro_id=` fragment anywhere in the recap text — carried there by
    `agent_chat_service._extract_recap_ids` via `find_cuenta_ids`, regardless
    of which tool produced it (e.g. `listar_cuentas_cobro`, not just
    `crear_cuenta_cobro`).

    Fallback signal (tool-name marker, kept for tools whose own dumped output
    doesn't literally carry a `cuenta_id`/`cuenta_cobro_id`-named key —
    `crear_cuenta_cobro` returns its OWN `id`, not a field named
    `cuenta_id`): a recap line reading `"{tool}:ok ..."` for
    `crear_cuenta_cobro` or for ANY cuenta-scoped tool is sufficient proof —
    those tools only ever succeed with a real cuenta id. The recap is built
    newest-first and can drop older entries once its char budget is exhausted
    (see `agent_chat_service._build_tool_context_recap`), so
    `crear_cuenta_cobro` itself might not literally be present on a
    long-running conversation — but if a cuenta exists and the conversation
    kept going, SOME later cuenta-scoped call almost certainly ran too, and
    that's what this fallback checks.
    """
    if not recap_text:
        return False
    if latest_cuenta_id_from_recap(recap_text) is not None:
        return True
    markers = cuenta_scoped_tool_names() | {"crear_cuenta_cobro"}
    return any(f"{name}:ok" in recap_text for name in markers)


def called_tool_names_from_recap(recap_text: str | None) -> frozenset[str]:
    """Orthogonal-gated tool names the recap shows already ran — success OR
    failure. CRITICAL 3: a FAILED call is even MORE reason to keep the tool
    visible for a retry on the next iteration than a successful one is."""
    if not recap_text:
        return frozenset()
    return frozenset(
        name for name in ORTHOGONAL_TOOL_KEYWORDS if f"{name}:ok" in recap_text or f"{name}:error" in recap_text
    )


def hidden_tool_names(
    *,
    message: str,
    cuenta_known: bool,
    called_tool_names: frozenset[str],
    recent_messages: Sequence[str] = (),
) -> frozenset[str]:
    """Return the tool names to EXCLUDE from this iteration's `tools=[...]`.

    `cuenta_known`: True once a real cuenta id has been observed anywhere in
    this conversation (cross-turn recap or an earlier successful call this
    turn) — see module docstring, gate 1.
    `called_tool_names`: every ORTHOGONAL-gated tool name that has already run
    (successfully OR failed — CRITICAL 3) at least once in this conversation
    (recap + this turn's own calls) — keeps it visible for follow-ups even if
    the CURRENT message no longer repeats the triggering keyword.
    `recent_messages`: a few PRIOR user messages (oldest-to-newest order
    doesn't matter — all are scanned) to also check for a triggering keyword,
    in addition to `message` itself. CRITICAL 3: the trigger phrase ("quiero
    registrar un otrosí") is often one message earlier than the current
    follow-up ("dale, hacelo") — checking only the current message missed
    this real conversational pattern entirely.
    """
    hidden: set[str] = set()

    if not cuenta_known:
        hidden |= cuenta_scoped_tool_names()

    lowered = " ".join((message, *recent_messages)).lower()
    for name, keywords in ORTHOGONAL_TOOL_KEYWORDS.items():
        if name in called_tool_names:
            continue
        if any(kw in lowered for kw in keywords):
            continue
        hidden.add(name)

    return frozenset(hidden)


def filter_openai_tools(tools: list[dict[str, Any]], hidden: frozenset[str]) -> list[dict[str, Any]]:
    """Drop any entry of `tools` (OpenAI `tools=[...]` shape) whose function
    name is in `hidden`. Returns `tools` unchanged (same list, not a copy)
    when `hidden` is empty — the common case — to avoid an allocation on every
    iteration of the loop for the (very common) fully-visible turn."""
    if not hidden:
        return tools
    return [t for t in tools if t.get("function", {}).get("name") not in hidden]
