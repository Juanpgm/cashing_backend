"""Tests for Phase 0 agent-turn observability (radicacion-sin-friccion):

- `ToolEvent.duration_ms` populated for both successful AND failed tool calls.
- `session_id` bound via `structlog.contextvars` for the lifetime of a turn,
  unbound afterward, and never leaked between concurrent turns on different
  sessions (contextvars are asyncio-task-local — see the concurrency test).
- The iteration-cap-hit path (existing `for...else`) still logs a final
  summary with the full iteration count and total turn duration.

`structlog.testing.capture_logs()` (the convention already used elsewhere in
this suite — see e.g. `test_secop_configuracion.py`) clears ALL configured
processors for its duration, including `structlog.contextvars.merge_contextvars`
— so a bound contextvar would NOT show up in captured events unless
`merge_contextvars` is reinstated ahead of the capturing processor. The
`_capture_logs_with_contextvars` helper below does exactly that.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Generator
from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
import structlog
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service
from sqlalchemy.ext.asyncio import AsyncSession


class ScriptedLLM:
    """Fake LLM adapter — returns pre-scripted `LLMResponse` objects in call order.

    Mirrors `tests/test_agent_chat_service_iterations.py::ScriptedLLM`; kept as a
    separate copy here (not imported) so this file has no cross-test-module
    coupling — a pattern already used elsewhere in this suite for fakes.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self._responses.pop(0)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: Any) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


@contextlib.contextmanager
def _capture_logs_with_contextvars() -> Generator[list[dict[str, Any]], None, None]:
    """Like `structlog.testing.capture_logs()`, but keeps `merge_contextvars`
    active so bound `structlog.contextvars` values actually appear in captured
    event dicts (plain `capture_logs()` strips every configured processor,
    `merge_contextvars` included).

    Mutates the EXISTING processors list in place (clear + append), exactly
    like `structlog.testing.capture_logs()` itself does — required because
    `app.main` configures structlog with `cache_logger_on_first_use=True`:
    already-used module loggers cache a reference to the ORIGINAL list object,
    so replacing it with a brand-new list (instead of mutating it) would be
    silently invisible to them.
    """
    cap = structlog.testing.LogCapture()
    processors = structlog.get_config()["processors"]
    old_processors = processors.copy()
    try:
        processors.clear()
        processors.append(structlog.contextvars.merge_contextvars)
        processors.append(cap)
        structlog.configure(processors=processors)
        yield cap.entries
    finally:
        processors.clear()
        processors.extend(old_processors)
        structlog.configure(processors=processors)




async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"obs_{suffix}@example.com",
        nombre=f"Obs User {suffix}",
        cedula=f"8181{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"OBS-{suffix}",
        objeto="Objeto de prueba para observabilidad",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"8181{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


class TestToolEventDuration:
    @pytest.mark.asyncio
    async def test_duration_ms_populated_and_positive_on_success(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user, contrato = await _make_user_with_contrato(db, "dur01")
        tool_call = LLMToolCall(
            id="call_1",
            name="crear_cuenta_cobro",
            arguments={"contrato_id": str(contrato.id), "mes": 7, "anio": 2026},
        )
        scripted = ScriptedLLM(
            [
                LLMResponse(content="", model="fake", tool_calls=[tool_call]),
                LLMResponse(content="Cuenta creada.", model="fake"),
            ]
        )
        _patch_llm(monkeypatch, scripted)

        result = await agent_chat_service.chat_with_tools(db, user, "Crea mi cuenta de julio", None, {})

        assert len(result.tool_events) == 1
        event = result.tool_events[0]
        assert event.status == "ok"
        assert event.duration_ms is not None
        assert event.duration_ms > 0

    @pytest.mark.asyncio
    async def test_duration_ms_populated_when_tool_raises(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edge case: a tool that raises must STILL get a `duration_ms` on its
        `ToolEvent` — timing is captured before the try/except, not only on the
        happy path."""
        user, _contrato = await _make_user_with_contrato(db, "dur02")
        tool_call = LLMToolCall(id="call_1", name="radicar_cuenta", arguments={"cuenta_id": str(uuid.uuid4())})
        scripted = ScriptedLLM(
            [
                LLMResponse(content="", model="fake", tool_calls=[tool_call]),
                LLMResponse(content="No se pudo radicar.", model="fake"),
            ]
        )
        _patch_llm(monkeypatch, scripted)

        result = await agent_chat_service.chat_with_tools(db, user, "Radica una cuenta inexistente", None, {})

        assert len(result.tool_events) == 1
        event = result.tool_events[0]
        assert event.status == "error"
        assert event.duration_ms is not None
        assert event.duration_ms >= 0


class TestSessionIdContextBinding:
    @pytest.mark.asyncio
    async def test_bound_session_id_appears_in_log_output_and_is_cleared_after(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user, _contrato = await _make_user_with_contrato(db, "ctx01")
        scripted = ScriptedLLM([LLMResponse(content="Hola! ¿En qué te ayudo?", model="fake")])
        _patch_llm(monkeypatch, scripted)

        with _capture_logs_with_contextvars() as captured:
            result = await agent_chat_service.chat_with_tools(db, user, "Hola", None, {})

        llm_turn_entries = [e for e in captured if e.get("event") == "agent_chat_llm_turn"]
        assert llm_turn_entries, "expected at least one agent_chat_llm_turn log entry"
        # This log call does NOT pass session_id explicitly — it only shows up here
        # because it's bound via structlog.contextvars, proving the binding works.
        assert all(e.get("session_id") == result.session_id for e in llm_turn_entries)

        # Unbound after the turn completes — must not leak into whatever runs next.
        assert "session_id" not in structlog.contextvars.get_contextvars()

    @pytest.mark.asyncio
    async def test_session_id_unbound_even_when_turn_raises(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `finally` around the bind must fire even on an unhandled exception,
        so a crashed turn never leaves a stale session_id bound for whatever code
        runs next on the same task/context."""

        user, _contrato = await _make_user_with_contrato(db, "ctx02")

        # `chat_with_tools`'s own `except Exception` around `llm.complete(...)`
        # only catches errors from THAT call — force the crash somewhere it does
        # NOT guard, by making `get_llm()` itself raise instead.
        def _raising_get_llm(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom - simulated crash before the loop")

        monkeypatch.setattr(agent_chat_service, "get_llm", _raising_get_llm)

        with pytest.raises(RuntimeError, match="boom"):
            await agent_chat_service.chat_with_tools(db, user, "Esto va a explotar", None, {})

        assert "session_id" not in structlog.contextvars.get_contextvars()


class TestConcurrentSessionsDoNotLeakContext:
    @pytest.mark.asyncio
    async def test_two_concurrent_turns_keep_their_own_session_id(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two chat turns for DIFFERENT sessions, running concurrently as separate
        asyncio Tasks, must never see each other's bound `session_id` — contextvars
        are asyncio-task-local, so this should hold "by construction" as long as
        the implementation binds via `structlog.contextvars` (a real
        `contextvars.ContextVar`) rather than some shared mutable state.
        """
        from sqlalchemy import select

        from tests.conftest import async_session_test

        user_a, _contrato_a = await _make_user_with_contrato(db, "ccA")
        user_b, _contrato_b = await _make_user_with_contrato(db, "ccB")

        class DualLLM:
            """Single shared fake LLM that answers based on which turn's user
            message it receives — lets both concurrent turns share one patched
            `get_llm()` return value without needing per-task dispatch state."""

            async def complete(self, messages: list[Any], **_kwargs: Any) -> LLMResponse:
                last_content = str(messages[-1].content)
                if "sesion-a" in last_content:
                    return LLMResponse(content="Respuesta A", model="fake")
                return LLMResponse(content="Respuesta B", model="fake")

        dual = DualLLM()
        # Patched ONCE for the whole test (not per-task with a manual save/restore):
        # both tasks resolve to the exact same `dual` instance, so there is no
        # meaningful "restore race" between them, and pytest's monkeypatch fixture
        # reverts this once the test ends either way.
        monkeypatch.setattr(agent_chat_service, "get_llm", lambda *a, **k: dual)

        # Each task binds its OWN independent `test_task` marker (a second,
        # test-only contextvar) alongside `chat_with_tools`'s own `session_id`
        # binding — this is the actual mechanism under test: if contextvars ever
        # leaked between concurrent tasks, `test_task` would show up paired with
        # the WRONG `session_id` in the captured entries below.
        async def _run_a() -> Any:
            structlog.contextvars.bind_contextvars(test_task="task-a")
            async with async_session_test() as session_a:
                # Re-attach the ORM objects created on `db` to this session by id
                # lookup — using the original Python objects directly here would
                # risk cross-session attachment issues; a fresh SELECT is safe.
                u = (await session_a.execute(select(Usuario).where(Usuario.id == user_a.id))).scalar_one()
                return await agent_chat_service.chat_with_tools(session_a, u, "mensaje sesion-a", None, {})

        async def _run_b() -> Any:
            structlog.contextvars.bind_contextvars(test_task="task-b")
            async with async_session_test() as session_b:
                u = (await session_b.execute(select(Usuario).where(Usuario.id == user_b.id))).scalar_one()
                return await agent_chat_service.chat_with_tools(session_b, u, "mensaje sesion-b", None, {})

        with _capture_logs_with_contextvars() as captured:
            result_a, result_b = await asyncio.gather(_run_a(), _run_b())

        assert result_a.session_id != result_b.session_id

        llm_turn_entries = [e for e in captured if e.get("event") == "agent_chat_llm_turn"]
        assert len(llm_turn_entries) == 2

        for entry in llm_turn_entries:
            if entry.get("test_task") == "task-a":
                assert entry.get("session_id") == result_a.session_id
            elif entry.get("test_task") == "task-b":
                assert entry.get("session_id") == result_b.session_id
            else:
                pytest.fail(f"unexpected test_task in captured log entry: {entry.get('test_task')!r}")

        # Explicit negative assertions — the real proof of NO cross-contamination.
        assert not any(
            e.get("test_task") == "task-a" and e.get("session_id") == result_b.session_id for e in llm_turn_entries
        )
        assert not any(
            e.get("test_task") == "task-b" and e.get("session_id") == result_a.session_id for e in llm_turn_entries
        )


class TestIterationCapFinalSummary:
    @pytest.mark.asyncio
    async def test_cap_hit_logs_final_summary_with_iteration_count_and_duration(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.tools.context import ToolContext
        from app.tools.invoke import invoke_tool

        user, contrato = await _make_user_with_contrato(db, "cap01")
        ctx = ToolContext(db=db, usuario=user)
        cuenta = await invoke_tool(
            "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 9, "anio": 2026}
        )
        await db.commit()

        responses = [
            LLMResponse(
                content="",
                model="fake",
                tool_calls=[
                    LLMToolCall(id=f"call_{i}", name="resumen_checklist", arguments={"cuenta_id": str(cuenta.id)})
                ],
            )
            for i in range(agent_chat_service.MAX_TOOL_ITERATIONS)
        ]
        scripted = ScriptedLLM(responses)
        _patch_llm(monkeypatch, scripted)

        with structlog.testing.capture_logs() as captured:
            result = await agent_chat_service.chat_with_tools(db, user, "Bucle infinito", None, {})

        assert "límite" in result.content.lower()

        summary_entries = [e for e in captured if e.get("event") == "agent_chat_tools"]
        assert summary_entries, "expected the final agent_chat_tools summary log even on cap-hit"
        entry = summary_entries[-1]
        assert entry["iterations_run"] == agent_chat_service.MAX_TOOL_ITERATIONS
        assert entry["turn_duration_ms"] >= 0
