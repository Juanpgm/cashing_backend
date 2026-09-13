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

from typing import Any

from app.tools.registry import TOOL_REGISTRY

_CUENTA_ID_FIELDS = ("cuenta_id", "cuenta_cobro_id")

# Tool name -> keyword substrings (lowercase) whose presence in the current
# user message re-exposes it even though nothing else in the conversation has
# used it yet. See module docstring, gate 2.
ORTHOGONAL_TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ingerir_plantilla_organismo": ("plantilla", "template"),
    "obtener_plantilla_organismo": ("plantilla", "template"),
    "listar_adiciones_contrato": ("adici",),  # adición / adicion / adicionar / adicionó
    "registrar_adicion_contrato": ("adici",),
}


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


def cuenta_known_from_recap(recap_text: str | None) -> bool:
    """Whether the cross-turn recap gives high-confidence evidence that a real
    cuenta de cobro already exists in this conversation.

    A recap line reading `"{tool}:ok ..."` for `crear_cuenta_cobro` or for ANY
    cuenta-scoped tool is sufficient proof — those tools only ever succeed
    with a real cuenta id. The recap is built newest-first and can drop older
    entries once its char budget is exhausted (see
    `agent_chat_service._build_tool_context_recap`), so `crear_cuenta_cobro`
    itself might not literally be present on a long-running conversation —
    but if a cuenta exists and the conversation kept going, SOME later
    cuenta-scoped call almost certainly ran too, and that's what this checks.
    """
    if not recap_text:
        return False
    markers = cuenta_scoped_tool_names() | {"crear_cuenta_cobro"}
    return any(f"{name}:ok" in recap_text for name in markers)


def called_tool_names_from_recap(recap_text: str | None) -> frozenset[str]:
    """Orthogonal-gated tool names the recap shows already ran successfully."""
    if not recap_text:
        return frozenset()
    return frozenset(name for name in ORTHOGONAL_TOOL_KEYWORDS if f"{name}:ok" in recap_text)


def hidden_tool_names(
    *,
    message: str,
    cuenta_known: bool,
    called_tool_names: frozenset[str],
) -> frozenset[str]:
    """Return the tool names to EXCLUDE from this iteration's `tools=[...]`.

    `cuenta_known`: True once a real cuenta id has been observed anywhere in
    this conversation (cross-turn recap or an earlier successful call this
    turn) — see module docstring, gate 1.
    `called_tool_names`: every ORTHOGONAL-gated tool name that has already run
    successfully at least once in this conversation (recap + this turn's own
    calls) — keeps it visible for follow-ups even if the CURRENT message no
    longer repeats the triggering keyword.
    """
    hidden: set[str] = set()

    if not cuenta_known:
        hidden |= cuenta_scoped_tool_names()

    lowered = message.lower()
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
