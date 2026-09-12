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
# `Settings._warn_if_secop_token_missing`'s `_log.warning(...)`, and
# `app.main`'s `structlog.configure(..., cache_logger_on_first_use=True)` caches
# that "core.config" logger's processor chain on its FIRST-ever call — if that
# first call happens here (outside of `structlog.testing.capture_logs()`), it
# poisons `tests/test_secop_configuracion.py::TestSecopTokenWarning`, which
# asserts on `capture_logs()` intercepting that exact warning. Keeping these
# `Settings(...)` constructions free of that side effect avoids the whole class
# of test-order-dependent flake.
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


async def test_chat_with_tools_completes_a_full_turn_with_llm_provider_fake(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED->GREEN target test: boots the REAL agent loop with LLM_PROVIDER=fake and
    asserts the turn completes end-to-end — no `bloquear_red_llm` opt-out needed
    (FakeLLMPort never imports litellm), no `ScriptedLLM` monkeypatch of `get_llm`
    (the real factory routes to the fake via the setting alone)."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    user, _contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(db, user, "Radicá mi cuenta de este mes", None, {})

    expected_sequence = [name for name in HAPPY_PATH_SEQUENCE.values() if name is not None]
    assert [event.tool for event in result.tool_events] == expected_sequence
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
