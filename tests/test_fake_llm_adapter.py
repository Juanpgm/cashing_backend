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
    HAPPY_PATH_SEQUENCE,
    FakeLLMPort,
    _last_tool_called,
    build_tool_call,
    synthesize_tool_arguments,
)
from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.core.config import Settings, settings
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.agent import LLMMessage
from app.services import agent_chat_service
from app.tools.registry import TOOL_REGISTRY
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


# --- Malformed / unrecognized input -> safe default, never a crash ----------


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
    """Boots the REAL agent loop with LLM_PROVIDER=fake and asserts the turn
    completes the whole scripted tool NAME sequence end-to-end — no
    `bloquear_red_llm` opt-out needed (FakeLLMPort never imports litellm), no
    `ScriptedLLM` monkeypatch of `get_llm` (the real factory routes to the fake
    via the setting alone).

    IMPORTANT: this does NOT assert a fully successful radicación chain. Only
    `listar_contratos` (the sole zero-required-argument tool in the sequence)
    actually succeeds. Every other tool call is built with `synthesize_tool_arguments`
    (see fake_adapter.py), which fills required fields with schema-valid but
    RANDOM placeholder values (e.g. `uuid.uuid4()` for foreign keys) — it does
    NOT thread real IDs from prior tool results (`crear_cuenta_cobro`'s real
    `contrato.id`, etc.). So the remaining 5 calls in this script correctly
    fail with domain "not found" errors against a real DB. This is a KNOWN,
    TRACKED limitation of the Phase 0 fake-adapter seam (cross-turn ID
    threading is future work, not part of this slice) — it is not a bug to fix
    here. The point of this test is that the fake adapter still drives the
    agent loop through the ENTIRE scripted tool-name sequence without crashing
    or stalling, regardless of each call's individual domain outcome.
    """
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    user, _contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(db, user, "Radicá mi cuenta de este mes", None, {})

    # Real observed outcome (verified by running this test): only the first
    # call (`listar_contratos`, the only tool with no required arguments) can
    # succeed against a real DB with synthesized/random arguments — the other
    # 5 calls fail with domain "not found" errors because their synthesized
    # foreign-key IDs are random UUIDs, not IDs threaded from prior results.
    expected_events = [
        ("listar_contratos", "ok"),
        ("crear_cuenta_cobro", "error"),
        ("definir_requisitos_checklist", "error"),
        ("importar_documento", "error"),
        ("resumen_checklist", "error"),
        ("radicar_cuenta", "error"),
    ]
    expected_sequence = [name for name in HAPPY_PATH_SEQUENCE.values() if name is not None]
    assert expected_sequence == [tool for tool, _status in expected_events]
    assert [(event.tool, event.status) for event in result.tool_events] == expected_events
    assert result.content
    assert result.session_id


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
