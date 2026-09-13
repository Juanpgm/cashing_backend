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
function of `HAPPY_PATH_SEQUENCE` and the last tool name seen in `messages`
(see `_last_tool_called`). The synthesized argument VALUES inside each tool
call are NOT deterministic — `synthesize_tool_arguments` calls `uuid.uuid4()`
for UUID fields and `datetime.date.today()` for date fields, so two runs of
the identical scripted sequence produce different argument payloads (and,
against a real DB, different domain outcomes per call — see
`tests/test_fake_llm_adapter.py::test_fake_llm_completes_the_full_tool_sequence_even_when_synthesized_ids_dont_resolve`).

Design: `complete()` is a PURE function of the `messages` history it receives —
no mutable instance state — so two concurrent chat sessions sharing (or each
holding their own) `FakeLLMPort` never leak script position into each other.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, get_args, get_origin

import structlog
from pydantic import BaseModel, ValidationError

from app.schemas.agent import LLMMessage, LLMResponse, LLMToolCall
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


def synthesize_tool_arguments(input_model: type[BaseModel]) -> dict[str, Any]:
    """Build a minimal, schema-VALID argument dict for `input_model`.

    Fills only the fields Pydantic requires (no default/default_factory) and
    leaves every optional field to its own default — mirrors how a real model
    would call a tool when the user hasn't specified optional knobs.
    """
    values: dict[str, Any] = {}
    for name, field_info in input_model.model_fields.items():
        if field_info.is_required():
            values[name] = _synthesize_field_value(field_info)
    return values


def build_tool_call(tool_name: str, input_model: type[BaseModel], call_id: str) -> LLMToolCall:
    """Build a schema-valid `LLMToolCall` for `tool_name`, validated against its
    own `input_model` before being handed back (fails loudly, at fake-adapter
    build time, if a future catalog tool needs a shape this synthesizer can't
    fill — never silently emits a call the real tool invoker would reject)."""
    arguments = synthesize_tool_arguments(input_model)
    input_model.model_validate(arguments)
    return LLMToolCall(id=call_id, name=tool_name, arguments=arguments)


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
        """
        del temperature, max_tokens, response_format, tool_choice, reasoning_effort, fallback
        used_model = model or self._default_model

        try:
            last_tool = _last_tool_called(messages)
        except Exception:
            await logger.awarning("fake_llm_malformed_history")
            return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)

        if last_tool not in HAPPY_PATH_SEQUENCE:
            # Unknown/unscripted position — e.g. a tool outside the happy path was
            # called, or the fake was dropped into a history it didn't build itself.
            return LLMResponse(content=_DEFAULT_TEXT_RESPONSE, model=used_model)

        next_tool = HAPPY_PATH_SEQUENCE[last_tool]
        if next_tool is None:
            return LLMResponse(content=_DONE_TEXT_RESPONSE, model=used_model)

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
        try:
            tool_call = build_tool_call(next_tool, spec.input_model, call_id=call_id)
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
