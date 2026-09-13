"""Tests for `FakeLLMPort` (app/adapters/llm/fake_adapter.py) — the deterministic,
network-free `LLMPort` implementation selected via `LLM_PROVIDER=fake`.

Deliberately does NOT use `tests/conftest.py`'s `bloquear_red_llm` guard or the
`ScriptedLLM` test-only pattern from `test_agent_chat_service_iterations.py` — this
suite tests the fake adapter itself (the seam the RUNNING APP uses for local dev /
future Playwright E2E runs), not pytest's own LLM-mocking convention.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
import structlog
from app.adapters.llm import get_llm
from app.adapters.llm.fake_adapter import (
    _DEFAULT_TEXT_RESPONSE,
    _GAVE_UP_TEXT_RESPONSE,
    HAPPY_PATH_SEQUENCE,
    FakeLLMPort,
    _known_ids,
    _last_tool_called,
    _tool_results,
    build_tool_call,
    synthesize_tool_arguments,
)
from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.core.config import Settings, settings
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.usuario import Usuario
from app.schemas.agent import LLMMessage
from app.services import agent_chat_service
from app.tools.registry import TOOL_REGISTRY
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# --- LLM_PROVIDER setting / get_llm() factory routing ------------------------


# `SECOP_APP_TOKEN` always passed non-empty below: an empty value trips
# `Settings._warn_if_secop_token_missing`'s `_log.warning(...)`, which is
# irrelevant noise for these LLM_PROVIDER-focused tests. This alone does NOT
# fully prevent test-order-dependent flakes on `app.core.config`'s shared
# module-level logger proxy (`_log`) — see the docstring on
# `test_llm_provider_invalid_value_logs_a_warning` below for the mechanism
# that actually matters: never `monkeypatch.setattr()` a method directly onto
# a structlog lazy-proxy object, only ever use `structlog.testing.capture_logs()`.
_NON_EMPTY_SECOP_TOKEN = "test-token-does-not-trigger-the-missing-token-warning"


def test_llm_provider_defaults_to_litellm() -> None:
    assert Settings(SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN).LLM_PROVIDER == "litellm"  # type: ignore[call-arg]


def test_llm_provider_invalid_value_falls_back_to_litellm() -> None:
    """An invalid env value must never crash Settings load nor silently leave
    LLM_PROVIDER pointing at an undefined provider — it normalizes to the safe
    default ("litellm"), the same real provider chain used when unset."""
    s = Settings(LLM_PROVIDER="banana", SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.LLM_PROVIDER == "litellm"


def test_llm_provider_fake_is_accepted() -> None:
    s = Settings(LLM_PROVIDER="fake", SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.LLM_PROVIDER == "fake"


@pytest.mark.parametrize("raw_value", ["FAKE", "Fake", " fake", "fake ", " FaKe "])
def test_llm_provider_fake_is_case_and_whitespace_insensitive(raw_value: str) -> None:
    """A typo'd casing/whitespace variant of "fake" must still route to the
    fake provider — otherwise it silently falls through to "litellm" (the
    REAL, network-calling provider), a real risk for a CI/E2E env that
    intended to stay network-free."""
    s = Settings(LLM_PROVIDER=raw_value, SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.LLM_PROVIDER == "fake"


# --- FAKE_LLM_SCRIPT setting (Phase 0, slice 0.6) ----------------------------


def test_fake_llm_script_defaults_to_none() -> None:
    s = Settings(SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.FAKE_LLM_SCRIPT is None


@pytest.mark.parametrize("raw_value", ["malformed", "MALFORMED", " Malformed "])
def test_fake_llm_script_accepts_malformed_case_and_whitespace_insensitive(raw_value: str) -> None:
    s = Settings(FAKE_LLM_SCRIPT=raw_value, SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.FAKE_LLM_SCRIPT == "malformed"


@pytest.mark.parametrize("raw_value", ["stall", "STALL", " stall "])
def test_fake_llm_script_accepts_stall_case_and_whitespace_insensitive(raw_value: str) -> None:
    s = Settings(FAKE_LLM_SCRIPT=raw_value, SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.FAKE_LLM_SCRIPT == "stall"


def test_fake_llm_script_accepts_happy_explicitly() -> None:
    s = Settings(FAKE_LLM_SCRIPT="happy", SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.FAKE_LLM_SCRIPT == "happy"


def test_fake_llm_script_invalid_value_falls_back_to_none() -> None:
    """An unrecognized value must never crash Settings load, nor silently pick a
    random script — it normalizes to `None`, i.e. the default happy path, mirroring
    `LLM_PROVIDER`'s own fold-to-default behavior for an invalid value."""
    s = Settings(FAKE_LLM_SCRIPT="banana", SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]
    assert s.FAKE_LLM_SCRIPT is None


def test_llm_provider_invalid_value_logs_a_warning() -> None:
    """An unrecognized value doesn't just silently fold to "litellm" — it logs
    a warning naming the invalid value received, so a misconfigured env var
    is discoverable instead of silently swallowed.

    Uses `structlog.testing.capture_logs()`, NOT `monkeypatch.setattr` on
    `app.core.config._log` directly: `_log` is a `structlog` lazy logger proxy
    that synthesizes attributes via `__getattr__` rather than storing them as
    real instance attributes, so `monkeypatch`'s teardown (which reads the "old"
    value via `getattr` to restore it later) ends up INSTALLING a permanently
    frozen bound method on `_log.warning` instead of removing the patch --
    verified: this poisoned `tests/test_secop_configuracion.py`'s
    `capture_logs()`-based assertions for every test file that runs
    alphabetically after this one, in the full suite (radicacion-sin-friccion
    0.4, third occurrence of this order-dependency class). `capture_logs()`
    mutates the shared processors list in place and cleans up correctly on
    exit -- no leak."""
    with structlog.testing.capture_logs() as captured:
        Settings(LLM_PROVIDER="banana", SECOP_APP_TOKEN=_NON_EMPTY_SECOP_TOKEN)  # type: ignore[call-arg]

    warn_events = [e for e in captured if e.get("event") == "llm_provider_invalid_value_fallback_to_litellm"]
    assert warn_events, "expected a queryable warning log for an unrecognized LLM_PROVIDER value"


def test_get_llm_returns_litellm_adapter_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "litellm")
    assert isinstance(get_llm(), LiteLLMAdapter)


def test_get_llm_returns_fake_llm_port_when_provider_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    assert isinstance(get_llm(), FakeLLMPort)


def test_get_llm_falls_back_to_litellm_for_a_bypassed_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a validator bypass — direct attribute mutation on the live `settings`
    singleton (the same technique `tests/conftest.py` already uses for other Settings
    fields) skips `_normalize_llm_provider`. `get_llm()`'s own explicit `== "fake"`
    check is a second line of defense: even a raw, un-normalized "banana" value must
    still route to the real provider, never to an undefined branch."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "banana")
    assert isinstance(get_llm(), LiteLLMAdapter)


# --- Routing (_last_tool_called / HAPPY_PATH_SEQUENCE) -----------------------


def test_last_tool_called_is_none_for_empty_or_tool_free_history() -> None:
    assert _last_tool_called([]) is None
    assert _last_tool_called([LLMMessage(role="user", content="hola")]) is None


def test_last_tool_called_reads_the_most_recent_assistant_tool_call() -> None:
    messages = [
        LLMMessage(role="user", content="hola"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "1", "type": "function", "function": {"name": "listar_contratos", "arguments": "{}"}}],
        ),
        LLMMessage(role="tool", tool_call_id="1", content="{}"),
    ]
    assert _last_tool_called(messages) == "listar_contratos"


# --- _tool_results / _known_ids: real cross-turn ID threading (Phase 0, 0.6) -


def _assistant_call(call_id: str, name: str) -> LLMMessage:
    return LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}],
    )


def test_tool_results_pairs_each_assistant_call_with_its_later_tool_message() -> None:
    messages = [
        LLMMessage(role="user", content="hola"),
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content='{"contratos": []}'),
        _assistant_call("2", "crear_cuenta_cobro"),
        LLMMessage(role="tool", tool_call_id="2", content='{"id": "not-checked-here"}'),
    ]
    results = _tool_results(messages)
    assert results == [
        ("1", "listar_contratos", {"contratos": []}),
        ("2", "crear_cuenta_cobro", {"id": "not-checked-here"}),
    ]


def test_tool_results_unpaired_assistant_call_is_not_included() -> None:
    """An assistant `tool_calls` message with no LATER `role=\"tool\"` message
    carrying a matching `tool_call_id` produces no result entry — the call hasn't
    resolved yet, so `_known_ids` has nothing to extract from it."""
    messages = [_assistant_call("1", "listar_contratos")]
    assert _tool_results(messages) == []


def test_known_ids_aliases_listar_contratos_result_into_contrato_id() -> None:
    real_id = str(uuid.uuid4())
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(
            role="tool",
            tool_call_id="1",
            content=json.dumps({"contratos": [{"id": real_id, "numero_contrato": "C-1"}]}),
        ),
    ]
    assert _known_ids(messages) == {"id": real_id, "contrato_id": real_id}


def test_known_ids_aliases_crear_cuenta_cobro_result_into_cuenta_id() -> None:
    cuenta_id = str(uuid.uuid4())
    contrato_id = str(uuid.uuid4())
    messages = [
        _assistant_call("1", "crear_cuenta_cobro"),
        LLMMessage(
            role="tool",
            tool_call_id="1",
            content=json.dumps({"id": cuenta_id, "contrato_id": contrato_id, "mes": 3}),
        ),
    ]
    known = _known_ids(messages)
    assert known["cuenta_id"] == cuenta_id
    assert known["contrato_id"] == contrato_id
    assert known["id"] == cuenta_id


def test_known_ids_two_tools_alias_to_different_keys_without_cross_contamination() -> None:
    """`listar_contratos` and `crear_cuenta_cobro` BOTH expose a generic top-level
    `id` key — each must alias into the field name the NEXT tool actually expects
    (`contrato_id` vs `cuenta_id`), never leak one tool's id under the other's
    target key."""
    contrato_id = str(uuid.uuid4())
    cuenta_id = str(uuid.uuid4())
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"contratos": [{"id": contrato_id}]})),
        _assistant_call("2", "crear_cuenta_cobro"),
        LLMMessage(role="tool", tool_call_id="2", content=json.dumps({"id": cuenta_id, "contrato_id": contrato_id})),
    ]
    known = _known_ids(messages)
    assert known["contrato_id"] == contrato_id
    assert known["cuenta_id"] == cuenta_id
    assert known["contrato_id"] != known["cuenta_id"]


def test_known_ids_missing_id_key_does_not_crash_and_yields_no_entry() -> None:
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"contratos": []})),
    ]
    assert _known_ids(messages) == {}


def test_known_ids_malformed_json_content_yields_empty_dict_no_exception() -> None:
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content="not json at all"),
    ]
    assert _known_ids(messages) == {}


def test_known_ids_parses_listar_contratos_real_compact_pipe_format() -> None:
    """`listar_contratos`'s REAL tool-result content in the running app is NOT
    JSON — `agent_chat_service._compact_listar_contratos` serializes it as
    `numero_contrato | entidad | valor_mensual | id` pipe-delimited plain text
    (bypassing the generic JSON path entirely for this tool). `_known_ids` must
    still recover the real `contrato_id` from that shape, not just from a
    hand-built JSON dict — otherwise the fix never actually threads IDs through
    the REAL running app, only through synthetic test histories."""
    real_id = str(uuid.uuid4())
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content=f"C-1 | Alcaldia | 1000000 | {real_id}"),
    ]
    assert _known_ids(messages)["contrato_id"] == real_id


def test_known_ids_definir_requisitos_checklist_aliases_cuenta_cobro_id() -> None:
    cuenta_id = str(uuid.uuid4())
    messages = [
        _assistant_call("1", "definir_requisitos_checklist"),
        LLMMessage(
            role="tool",
            tool_call_id="1",
            content=json.dumps({"cuenta_cobro_id": cuenta_id, "modo": "estandar", "requisitos_custom": 0}),
        ),
    ]
    assert _known_ids(messages)["cuenta_id"] == cuenta_id


def test_known_ids_resumen_checklist_aliases_cuenta_cobro_id() -> None:
    cuenta_id = str(uuid.uuid4())
    messages = [
        _assistant_call("1", "resumen_checklist"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"cuenta_cobro_id": cuenta_id})),
    ]
    assert _known_ids(messages)["cuenta_id"] == cuenta_id


def test_synthesize_tool_arguments_uses_known_id_for_matching_uuid_field() -> None:
    from app.tools.catalog.cuentas import CrearCuentaCobroInput

    real_contrato_id = str(uuid.uuid4())
    args = synthesize_tool_arguments(CrearCuentaCobroInput, known={"contrato_id": real_contrato_id})
    assert args["contrato_id"] == real_contrato_id


def test_synthesize_tool_arguments_ignores_known_when_field_not_present() -> None:
    from app.tools.catalog.cuentas import CrearCuentaCobroInput

    args = synthesize_tool_arguments(CrearCuentaCobroInput, known={"cuenta_id": str(uuid.uuid4())})
    assert "cuenta_id" not in args
    # contrato_id still synthesized (falls back to a random UUID, today's behavior)
    uuid.UUID(args["contrato_id"])


def test_synthesize_tool_arguments_known_none_default_matches_pre_existing_behavior() -> None:
    """Explicit regression proof for the `known: dict[str, str] | None = None`
    default — every field must still be synthesized exactly like before this
    slice when no `known` dict is passed at all."""
    from app.tools.catalog.cuentas import CrearCuentaCobroInput

    args = synthesize_tool_arguments(CrearCuentaCobroInput)
    assert set(args) == {"contrato_id", "mes", "anio"}
    uuid.UUID(args["contrato_id"])


# --- Malformed / unrecognized input -> safe default, never a crash ----------


async def test_complete_threads_real_contrato_id_into_crear_cuenta_cobro_call() -> None:
    """The core bug this slice fixes: `crear_cuenta_cobro`'s synthesized
    `contrato_id` must be the REAL id `listar_contratos` returned in this same
    history, not a fresh random `uuid.uuid4()`."""
    fake = FakeLLMPort()
    real_contrato_id = str(uuid.uuid4())
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(
            role="tool",
            tool_call_id="1",
            content=json.dumps({"contratos": [{"id": real_contrato_id, "numero_contrato": "C-1"}]}),
        ),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "crear_cuenta_cobro"
    assert response.tool_calls[0].arguments["contrato_id"] == real_contrato_id


async def test_complete_retries_the_same_tool_once_after_a_failed_result() -> None:
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]
    messages = [
        _assistant_call("1", "crear_cuenta_cobro"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"error": "boom"})),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "crear_cuenta_cobro"


async def test_complete_gives_up_after_a_retry_also_fails() -> None:
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]
    messages = [
        _assistant_call("1", "crear_cuenta_cobro"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"error": "boom"})),
        _assistant_call("2", "crear_cuenta_cobro"),
        LLMMessage(role="tool", tool_call_id="2", content=json.dumps({"error": "boom again"})),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is None
    assert response.content


async def test_complete_advances_normally_when_last_result_succeeded() -> None:
    """Sibling of the retry test above: a SUCCESSFUL paired result must still
    advance `HAPPY_PATH_SEQUENCE` normally (outcome-aware routing must not
    accidentally start retrying successful calls too)."""
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"contratos": []})),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "crear_cuenta_cobro"


# --- response_format structured output (Phase 0, slice 0.6) -----------------


async def test_complete_honors_response_format_and_returns_valid_structured_json() -> None:
    """Every real `response_format=` call site (extraction.py, document_service.py,
    checklist_service.py, requisito_inference_service.py) expects a JSON string
    matching the given Pydantic model back in `response.content` — NOT a tool
    call, NOT prose. This is a general-mechanism proof using a test-local dummy
    model (mirrors `test_complete_degrades_safely_when_synthesized_arguments_fail_validation`'s
    pattern) — `_synthesize_field_value` doesn't handle nested BaseModel fields,
    so a REAL response_format model with a required nested model field would
    still degrade safely (see the sibling test below) rather than produce valid
    output; this proves the MECHANISM works for the shapes it does support."""

    class _DummyStructuredOutput(BaseModel):
        resumen: str
        confianza: int

    fake = FakeLLMPort()
    response = await fake.complete([LLMMessage(role="user", content="hola")], response_format=_DummyStructuredOutput)
    assert response.tool_calls is None
    parsed = json.loads(response.content)
    _DummyStructuredOutput.model_validate(parsed)


async def test_complete_response_format_degrades_safely_when_unsynthesizable() -> None:
    class _UnsynthesizableStructuredOutput(BaseModel):
        payload: bytes  # not a shape _synthesize_field_value knows how to fill

    fake = FakeLLMPort()
    response = await fake.complete(
        [LLMMessage(role="user", content="hola")], response_format=_UnsynthesizableStructuredOutput
    )
    assert response.tool_calls is None
    assert response.content == _DEFAULT_TEXT_RESPONSE


# --- FAKE_LLM_SCRIPT=stall / malformed (Phase 0, slice 0.6) -----------------


async def test_complete_stall_script_always_returns_text_never_a_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FAKE_LLM_SCRIPT=stall` stalls UNCONDITIONALLY — even mid-sequence, with a
    history that would normally advance to a real scripted tool call — proving
    the "agent gave up mid-chain" edge case regardless of routing state."""
    monkeypatch.setattr(settings, "FAKE_LLM_SCRIPT", "stall")
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(role="tool", tool_call_id="1", content=json.dumps({"contratos": []})),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is None
    assert response.content


async def test_chat_with_tools_stall_script_completes_one_turn_without_progress(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(settings, "FAKE_LLM_SCRIPT", "stall")
    user, _contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(db, user, "Radicá mi cuenta", None, {})

    assert result.tool_events == []
    assert result.content
    assert result.session_id


async def test_complete_malformed_script_corrupts_crear_cuenta_cobro_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FAKE_LLM_SCRIPT=malformed` deliberately emits a `mes` value (13) that
    violates `CrearCuentaCobroInput.mes`'s `Field(ge=1, le=12)` — proving it
    genuinely fails the tool's OWN schema (not just "some invalid string"),
    and that the fake SKIPS its own `build_tool_call` self-check for this one
    call (a real provider's tool-call arguments aren't pre-validated either)."""
    from app.tools.catalog.cuentas import CrearCuentaCobroInput

    monkeypatch.setattr(settings, "FAKE_LLM_SCRIPT", "malformed")
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]
    messages = [
        _assistant_call("1", "listar_contratos"),
        LLMMessage(
            role="tool",
            tool_call_id="1",
            content=json.dumps({"contratos": [{"id": str(uuid.uuid4())}]}),
        ),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "crear_cuenta_cobro"
    assert response.tool_calls[0].arguments["mes"] == 13
    with pytest.raises(ValidationError):
        CrearCuentaCobroInput.model_validate(response.tool_calls[0].arguments)


async def test_chat_with_tools_malformed_script_surfaces_a_validation_error_not_a_network_failure(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traces the malformed tool call THROUGH the real `agent_chat_service.
    chat_with_tools` loop (not just asserting the fake returns bad JSON): the
    resulting `ToolEvent` for `crear_cuenta_cobro` must be a genuine pydantic
    `ValidationError`-shaped failure (`_format_tool_error`'s ValidationError
    branch, naming the bad field) — per slice 0.4's own guidance, this must
    NEVER be misdiagnosed as an LLM-network failure (the
    "No pude contactar al modelo..." message from `chat_with_tools`'s outer
    `except Exception` around `llm.complete()`)."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(settings, "FAKE_LLM_SCRIPT", "malformed")
    user, _contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(db, user, "Radicá mi cuenta de este mes", None, {})

    # `FAKE_LLM_SCRIPT=malformed` corrupts `crear_cuenta_cobro`'s arguments
    # EVERY time it's the scripted next step — including the outcome-aware
    # retry (also this slice) — so it fails TWICE (initial attempt + retry)
    # before the fake gives up, same shape as the plain retry-once test above.
    crear_events = [e for e in result.tool_events if e.tool == "crear_cuenta_cobro"]
    assert len(crear_events) == 2
    assert all(e.status == "error" for e in crear_events)
    assert all("mes" in e.resumen for e in crear_events)
    assert "No pude contactar al modelo" not in result.content


# --- Cross-turn recap resume (Phase 0, slice 0.6) ---------------------------


async def test_complete_resumes_from_recap_when_no_tool_calls_in_history() -> None:
    """A CONTINUATION turn on the same session_id replays history with the
    cross-turn recap (`agent_chat_service._build_tool_context_recap`'s exact
    format — role="system", `AGENT_RECAP_MARKER` prefix, `tool:status k=v`
    entries) but NO assistant `tool_calls` at all (only the user + assistant
    text from the persisted `Conversacion.mensajes_json`). The fake must
    resume `HAPPY_PATH_SEQUENCE` from the recap's newest entry AND thread its
    ids into the next call's arguments."""
    from app.schemas.agent import AGENT_RECAP_MARKER

    cuenta_id = str(uuid.uuid4())
    contrato_id = str(uuid.uuid4())
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "definir_requisitos_checklist"}}]
    messages = [
        LLMMessage(
            role="system",
            content=f"{AGENT_RECAP_MARKER} crear_cuenta_cobro:ok id={cuenta_id} contrato_id={contrato_id}",
        ),
        LLMMessage(role="user", content="creá la cuenta"),
        LLMMessage(role="assistant", content="Listo, la cuenta quedó creada."),
        LLMMessage(role="user", content="seguí con el checklist"),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "definir_requisitos_checklist"
    assert response.tool_calls[0].arguments["cuenta_id"] == cuenta_id


async def test_complete_falls_back_to_fresh_start_on_malformed_recap() -> None:
    """A truncated/garbled recap line (never happens today — `_RECAP_MAX_CHARS`
    truncates safely — but a hand-built or future history could still carry
    one) must never raise, and must fall back to starting the sequence fresh,
    same as no history/recap at all."""
    from app.schemas.agent import AGENT_RECAP_MARKER

    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "listar_contratos"}}]
    messages = [
        LLMMessage(role="system", content=f"{AGENT_RECAP_MARKER} garbled;;not-the-expected-format###"),
        LLMMessage(role="user", content="seguí"),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "listar_contratos"


async def test_complete_recap_picks_the_newest_entry_when_multiple_lines_present() -> None:
    """Recap lines are newest-first (`_build_tool_context_recap` builds from
    `reversed(call_results)`) — the resume position must come from the FIRST
    (most recent) `ok` entry, not an older one."""
    from app.schemas.agent import AGENT_RECAP_MARKER

    newer_cuenta_id = str(uuid.uuid4())
    older_contrato_id = str(uuid.uuid4())
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "definir_requisitos_checklist"}}]
    messages = [
        LLMMessage(
            role="system",
            content=(
                f"{AGENT_RECAP_MARKER} crear_cuenta_cobro:ok id={newer_cuenta_id} "
                f"| listar_contratos:ok id={older_contrato_id}"
            ),
        ),
        LLMMessage(role="user", content="seguí"),
    ]
    response = await fake.complete(messages, tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "definir_requisitos_checklist"
    assert response.tool_calls[0].arguments["cuenta_id"] == newer_cuenta_id


async def test_complete_with_malformed_tool_call_shape_returns_safe_default() -> None:
    """A `function` value that isn't a dict (can't happen via the real LLMPort
    contract, but nothing stops a hand-built or third-party history from doing it)
    must degrade to a plain text reply, not raise."""
    fake = FakeLLMPort()
    messages = [
        LLMMessage(role="user", content="hola"),
        LLMMessage(role="assistant", content="", tool_calls=[{"function": "not-a-dict"}]),
    ]
    response = await fake.complete(messages)
    assert response.tool_calls is None
    assert response.content


async def test_complete_with_unscripted_tool_name_returns_safe_default() -> None:
    """A tool name outside `HAPPY_PATH_SEQUENCE` (e.g. a different LLM/fixture
    called something else earlier in the same history) is a safe default too,
    never a KeyError."""
    fake = FakeLLMPort()
    messages = [
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "1", "type": "function", "function": {"name": "algo_no_scripteado", "arguments": "{}"}}],
        ),
    ]
    response = await fake.complete(messages)
    assert response.tool_calls is None
    assert response.content


async def test_complete_does_not_request_a_tool_the_caller_did_not_offer() -> None:
    """If the caller's `tools=[...]` doesn't include the scripted next tool, never
    ask for it anyway — degrade to plain text instead of a call the real tool
    invoker couldn't dispatch."""
    fake = FakeLLMPort()
    response = await fake.complete([], tools=[{"type": "function", "function": {"name": "otra_herramienta"}}])
    assert response.tool_calls is None


async def test_complete_with_malformed_tools_list_entry_returns_safe_default() -> None:
    """A `tools=[...]` entry whose `function` value isn't a dict (the sibling
    malformed shape to `test_complete_with_malformed_tool_call_shape_returns_safe_default`,
    but on the OFFERED-tools list rather than a prior `tool_calls` message) must
    degrade to a plain text reply too, not raise `AttributeError` from
    `.get("name")` on a non-dict."""
    fake = FakeLLMPort()
    response = await fake.complete([], tools=[{"function": "not-a-dict"}])
    assert response.tool_calls is None
    assert response.content


async def test_complete_degrades_safely_when_synthesized_arguments_fail_validation() -> None:
    """If `synthesize_tool_arguments` can't fill a required field shape (e.g. a
    hypothetical strict-schema tool with a type this synthesizer doesn't know
    how to fill), `build_tool_call`'s own `model_validate` raises
    `ValidationError` — `complete()` must catch it and degrade to a safe
    plain-text reply, not let it propagate up where the caller
    (`agent_chat_service.chat_with_tools`) would misdiagnose it as an
    LLM-network failure rather than a fake-adapter synthesis gap."""
    from app.tools.registry import ToolSpec
    from pydantic import BaseModel

    class _UnsynthesizableInput(BaseModel):
        # `bytes` isn't one of the shapes `_synthesize_field_value` knows how to
        # fill — it falls back to `None`, which this required field rejects.
        payload: bytes

    class _DummyOutput(BaseModel):
        ok: bool = True

    async def _unused_handler(_ctx: object, _args: object) -> _DummyOutput:
        raise AssertionError("handler should never be invoked in this test")

    hypothetical_spec = ToolSpec(
        name="listar_contratos",
        description="test-only strict-schema stand-in",
        input_model=_UnsynthesizableInput,
        output_model=_DummyOutput,
        handler=_unused_handler,  # type: ignore[arg-type]
    )

    original_spec = TOOL_REGISTRY["listar_contratos"]
    TOOL_REGISTRY["listar_contratos"] = hypothetical_spec
    try:
        fake = FakeLLMPort()
        tools = [{"type": "function", "function": {"name": "listar_contratos"}}]
        response = await fake.complete([LLMMessage(role="user", content="hola")], tools=tools)
    finally:
        TOOL_REGISTRY["listar_contratos"] = original_spec

    assert response.tool_calls is None
    assert response.content


# --- Happy-path script produces schema-valid, JSON-serializable calls -------


async def test_complete_first_turn_calls_listar_contratos() -> None:
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "listar_contratos"}}]
    response = await fake.complete([LLMMessage(role="user", content="hola")], tools=tools)
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "listar_contratos"
    assert response.tool_calls[0].arguments == {}


async def test_complete_advances_through_the_full_happy_path_sequence() -> None:
    fake = FakeLLMPort()
    all_tool_names = [name for name in HAPPY_PATH_SEQUENCE.values() if name is not None]
    tools = [{"type": "function", "function": {"name": name}} for name in all_tool_names]

    messages: list[LLMMessage] = [LLMMessage(role="user", content="radicar todo")]
    called: list[str] = []
    for _ in range(len(all_tool_names) + 1):
        response = await fake.complete(messages, tools=tools)
        if not response.tool_calls:
            break
        call = response.tool_calls[0]
        called.append(call.name)
        # Round-trip through json.dumps/loads exactly like the real agent loop does
        # when persisting the assistant message (agent_chat_service.chat_with_tools).
        json.dumps(call.arguments)
        messages.append(
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": "{}"}}],
            )
        )
        messages.append(LLMMessage(role="tool", tool_call_id=call.id, content="{}"))

    assert called == all_tool_names


async def test_stream_yields_the_same_content_as_complete() -> None:
    fake = FakeLLMPort()
    messages = [LLMMessage(role="user", content="hola")]
    complete_response = await fake.complete(messages)

    chunks = [chunk async for chunk in fake.stream(messages)]
    assert chunks == [complete_response.content]


async def test_embed_returns_one_vector_per_text() -> None:
    fake = FakeLLMPort()
    vectors = await fake.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert all(isinstance(v, list) and v for v in vectors)


# --- Concurrency: stateless design must not leak state across sessions ------


async def test_concurrent_calls_do_not_leak_state_between_sessions() -> None:
    """Two 'sessions' at DIFFERENT points in the script, called concurrently on
    the SAME `FakeLLMPort` instance, must each get the response for THEIR OWN
    history — proving `complete()` is a pure function of `messages`, not stateful
    instance-level position tracking."""
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": name}} for name in TOOL_REGISTRY]

    session_a_messages = [LLMMessage(role="user", content="turno inicial")]
    session_b_messages = [
        LLMMessage(role="user", content="ya avancé"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "1", "type": "function", "function": {"name": "listar_contratos", "arguments": "{}"}}],
        ),
        LLMMessage(role="tool", tool_call_id="1", content="{}"),
    ]

    results = await asyncio.gather(
        *[fake.complete(session_a_messages, tools=tools) for _ in range(5)],
        *[fake.complete(session_b_messages, tools=tools) for _ in range(5)],
    )
    a_results, b_results = results[:5], results[5:]

    assert all(r.tool_calls is not None and r.tool_calls[0].name == "listar_contratos" for r in a_results)
    assert all(r.tool_calls is not None and r.tool_calls[0].name == "crear_cuenta_cobro" for r in b_results)


async def test_concurrent_calls_do_not_leak_known_ids_between_sessions() -> None:
    """Sibling of the routing-leak test above, but for `known` IDS specifically
    (the new slice 0.6 feature) — two sessions concurrently at the SAME routing
    position (`crear_cuenta_cobro` next) but with DIFFERENT real `contrato_id`
    values in their own history must each synthesize THEIR OWN id, never the
    other session's — proving `_known_ids`/`synthesize_tool_arguments(known=)`
    introduced no module-level mutable state either."""
    fake = FakeLLMPort()
    tools = [{"type": "function", "function": {"name": "crear_cuenta_cobro"}}]

    contrato_id_a = str(uuid.uuid4())
    contrato_id_b = str(uuid.uuid4())

    def _messages_for(contrato_id: str) -> list[LLMMessage]:
        return [
            _assistant_call("1", "listar_contratos"),
            LLMMessage(
                role="tool",
                tool_call_id="1",
                content=json.dumps({"contratos": [{"id": contrato_id}]}),
            ),
        ]

    session_a_messages = _messages_for(contrato_id_a)
    session_b_messages = _messages_for(contrato_id_b)

    results = await asyncio.gather(
        *[fake.complete(session_a_messages, tools=tools) for _ in range(5)],
        *[fake.complete(session_b_messages, tools=tools) for _ in range(5)],
    )
    a_results, b_results = results[:5], results[5:]

    assert all(
        r.tool_calls is not None and r.tool_calls[0].arguments["contrato_id"] == contrato_id_a for r in a_results
    )
    assert all(
        r.tool_calls is not None and r.tool_calls[0].arguments["contrato_id"] == contrato_id_b for r in b_results
    )


# --- Full catalog schema validity (all 32 tools, not just the playbook) -----


@pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY))
def test_synthesized_arguments_are_schema_valid_for_every_catalog_tool(tool_name: str) -> None:
    """Parametrized over the WHOLE 32-tool `TOOL_REGISTRY` (not just the ~6-tool
    happy-path script) — the fake must be ABLE to produce a schema-valid call for
    any tool, even though the scripted sequence only exercises a handful."""
    spec = TOOL_REGISTRY[tool_name]
    call = build_tool_call(tool_name, spec.input_model, call_id="test-call")

    assert call.name == tool_name
    # Must round-trip through JSON exactly like the real agent loop's
    # `json.dumps(call.arguments)` (agent_chat_service.chat_with_tools) —
    # a native uuid.UUID/Decimal/date value would raise TypeError here.
    reloaded = json.loads(json.dumps(call.arguments))
    spec.input_model.model_validate(reloaded)


def test_synthesize_tool_arguments_only_fills_required_fields() -> None:
    from app.tools.catalog.cuentas import CrearCuentaCobroInput

    args = synthesize_tool_arguments(CrearCuentaCobroInput)
    assert set(args) == {"contrato_id", "mes", "anio"}
    assert 1 <= args["mes"] <= 12
    assert 2000 <= args["anio"] <= 2099


# --- Integration: a full chat turn completes via the REAL agent loop -------
# with NO litellm mock/monkeypatch of get_llm — LLM_PROVIDER=fake alone routes
# `agent_chat_service.chat_with_tools`'s `get_llm()` call to `FakeLLMPort`.


async def _make_user_with_contrato(db: AsyncSession) -> tuple[Usuario, Contrato]:
    suffix = uuid.uuid4().hex[:8]
    user = Usuario(
        email=f"fake_llm_{suffix}@example.com",
        nombre=f"Fake LLM User {suffix}",
        cedula=f"77{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"FAKE-{suffix}",
        objeto="Objeto de prueba para FakeLLMPort",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"77{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def test_fake_llm_completes_the_full_tool_sequence_even_when_synthesized_ids_dont_resolve(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boots the REAL agent loop with LLM_PROVIDER=fake — no `bloquear_red_llm`
    opt-out needed (FakeLLMPort never imports litellm), no `ScriptedLLM`
    monkeypatch of `get_llm` (the real factory routes to the fake via the
    setting alone).

    UPDATED for slice 0.6 (real cross-turn ID threading + outcome-aware
    routing) — the docstring below describes the CURRENT, verified outcome;
    the previous version of this test (pre-0.6) asserted only 1/6 calls could
    ever succeed, which was exactly the threading gap this slice fixes.

    Real observed outcome (verified by running this test): the first THREE
    calls now genuinely succeed against the real DB — `listar_contratos`
    discovers the seeded contrato, `crear_cuenta_cobro` receives its REAL
    `contrato_id` (not a random `uuid4()` — see the DB assertion below, which
    is the direct proof `FakeLLMPort._known_ids`/`known=` actually threaded
    it), and `definir_requisitos_checklist` receives the REAL `cuenta_id`
    `crear_cuenta_cobro` just created. `importar_documento` then fails twice
    (an initial attempt + one outcome-aware retry, also this slice) — that
    failure is NOT an ID-threading gap: this test supplies NO chat attachment,
    so `synthesize_tool_arguments` can only synthesize a `filename` STRING
    ("fake"), never a real uploaded file, and `ctx.attachments.get("fake")` is
    always `None` regardless of ID threading (see
    `app/tools/catalog/importar_documento.py`). Exercising a real file upload
    end-to-end is a different tool's concern, out of scope here. `radicar_cuenta`
    (script step 6) is correctly never reached — the chain gives up after
    `importar_documento`'s retry also fails, instead of looping forever.
    """
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    user, contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(db, user, "Radicá mi cuenta de este mes", None, {})

    expected_events = [
        ("listar_contratos", "ok"),
        ("crear_cuenta_cobro", "ok"),
        ("definir_requisitos_checklist", "ok"),
        ("importar_documento", "error"),
        ("importar_documento", "error"),
    ]
    assert [(event.tool, event.status) for event in result.tool_events] == expected_events
    assert result.content == _GAVE_UP_TEXT_RESPONSE
    assert result.session_id

    # `importar_documento`'s failures each trigger `db.rollback()` inside
    # `chat_with_tools`, which expires every object in the shared `db` session
    # (regardless of `expire_on_commit`) — refresh `contrato` (created earlier,
    # outside that rollback) before reading its attributes, exactly like the
    # service itself does for its own long-lived objects after a rollback (see
    # `test_chat_with_tools_fake_llm_is_deterministic_across_runs` below).
    await db.refresh(contrato)

    # Direct DB proof of real ID threading: the cuenta de cobro `crear_cuenta_cobro`
    # created must be linked to the SAME contrato `listar_contratos` discovered — if
    # `contrato_id` had been a random uuid4() (the pre-0.6 bug), `crear_cuenta_cobro`
    # would have failed with "Contrato not found" and no row would exist here at all.
    cuenta_result = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    cuenta = cuenta_result.scalar_one()
    assert cuenta.contrato_id == contrato.id
    assert cuenta.requisitos_modo is not None  # definir_requisitos_checklist ran too


async def test_chat_with_tools_fake_llm_is_deterministic_across_runs(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two independent conversations (fresh session_id each) driven by the SAME
    `LLM_PROVIDER=fake` must produce the identical tool-call SEQUENCE — the whole
    point of the fake for future Playwright E2E runs is a repeatable script."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    user_1, _ = await _make_user_with_contrato(db)
    user_2, _ = await _make_user_with_contrato(db)

    result_1 = await agent_chat_service.chat_with_tools(db, user_1, "Radicá mi cuenta", None, {})
    # A failed tool call inside chat_with_tools rolls back and expires every object
    # on the shared `db` session (see agent_chat_service.chat_with_tools's per-call
    # exception handler) — refresh user_2 (created earlier, unrelated to that
    # rollback) before reusing it, exactly like the service itself does for its own
    # long-lived objects after a rollback.
    await db.refresh(user_2)
    result_2 = await agent_chat_service.chat_with_tools(db, user_2, "Radicá mi cuenta", None, {})

    assert [e.tool for e in result_1.tool_events] == [e.tool for e in result_2.tool_events]
    assert result_1.content == result_2.content
