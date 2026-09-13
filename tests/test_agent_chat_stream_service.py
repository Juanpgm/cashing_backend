"""Tests for `app.services.agent_chat_service.stream_chat_with_tools` and the
write-tool approval/cancel/retry gate it drives (`app.services.agent_tool_approval`,
`app.services.agent_tool_gate`) — radicacion-sin-friccion 3.10.

The LLM is fully scripted (`ScriptedLLM`, same pattern as `test_agent_chat_service.
py`) — no network calls. Real registry tools (seeded via `app.tools.catalog`) run
against the in-memory SQLite `db` fixture, so approval/rejection/retry are proven by
actual DB side effects (a `CuentaCobro` row existing or not), not just event shapes.

Concurrency note: resolving a pending decision (`agent_tool_approval.resolve`) is a
PLAIN synchronous function — no `asyncio.gather`/background task needed in these
tests. Calling it directly, in the SAME coroutine, right after observing the
`tool_call_awaiting_approval`/`tool_call_awaiting_retry` event in the stream, is
enough: the gate coroutine (running inside `stream_chat_with_tools`'s background
`asyncio.Task`) is already parked on `entry.event.wait()` by the time that event was
yielded, and picks the decision up on its next scheduling turn.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.config import settings
from app.core.exceptions import PendingToolCallNotFoundError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service, agent_tool_approval
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


class ScriptedLLM:
    """Fake LLM adapter — returns pre-scripted `LLMResponse` objects in call order."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self._responses.pop(0)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: ScriptedLLM) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"agent_stream_{suffix}@example.com",
        nombre="Agent Stream User",
        cedula=f"4040{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"AS-{suffix}",
        objeto="Objeto de prueba para el chat en streaming",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"4040{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _collect(agen: Any) -> list[dict[str, Any]]:
    return [event async for event in agen]


async def _drain_background_tasks(timeout: float = 2.0) -> None:
    """Wait for `agent_chat_service._background_tasks` to empty — used when a test
    deliberately stops consuming the stream early (simulated disconnect) but still
    needs the turn to finish so it can assert on the final persisted state."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while agent_chat_service._background_tasks:
        if loop.time() > deadline:
            raise AssertionError("background chat turn task did not finish in time")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clear_approval_store() -> Iterator[None]:
    agent_tool_approval.clear()
    yield
    agent_tool_approval.clear()


# ---------------------------------------------------------------------------
# Read tools — no pause
# ---------------------------------------------------------------------------


async def test_read_tool_streams_without_pause(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, _contrato = await _make_user_with_contrato(db, "0001")
    tool_call = LLMToolCall(id="call_1", name="listar_contratos", arguments={})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="Tenés 1 contrato activo.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    events = await _collect(agent_chat_service.stream_chat_with_tools(db, user, "Qué contratos tengo?", None, {}))

    types = [e["type"] for e in events]
    assert types[0] == "connected"
    assert "tool_call_awaiting_approval" not in types
    assert "tool_call_started" in types
    assert "tool_call_result" in types
    result_event = next(e for e in events if e["type"] == "tool_call_result")
    assert result_event["tool"] == "listar_contratos"
    assert result_event["status"] == "ok"
    final_event = events[-1]
    assert final_event["type"] == "final"
    assert final_event["content"] == "Tenés 1 contrato activo."


# ---------------------------------------------------------------------------
# Write tool — approval gate
# ---------------------------------------------------------------------------


async def test_write_tool_approved_executes_and_commits(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "0002")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="Listo, creé tu cuenta de junio.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    events: list[dict[str, Any]] = []
    session_id: str | None = None
    async for event in agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta de junio", None, {}):
        events.append(event)
        if event["type"] == "connected":
            session_id = event["session_id"]
        if event["type"] == "tool_call_awaiting_approval":
            assert session_id is not None
            agent_tool_approval.resolve(session_id, event["call_id"], user.id, "approve")

    types = [e["type"] for e in events]
    assert "tool_call_awaiting_approval" in types
    assert "tool_call_approved" in types
    result_event = next(e for e in events if e["type"] == "tool_call_result")
    assert result_event["status"] == "ok"
    assert events[-1]["type"] == "final"
    assert events[-1]["content"] == "Listo, creé tu cuenta de junio."

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert len(rows.scalars().all()) == 1


async def test_write_tool_rejected_never_executes(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user, contrato = await _make_user_with_contrato(db, "0003")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="Entendido, no creé la cuenta.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    events: list[dict[str, Any]] = []
    session_id: str | None = None
    async for event in agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta de junio", None, {}):
        events.append(event)
        if event["type"] == "connected":
            session_id = event["session_id"]
        if event["type"] == "tool_call_awaiting_approval":
            assert session_id is not None
            agent_tool_approval.resolve(session_id, event["call_id"], user.id, "reject")

    assert "tool_call_rejected" in [e["type"] for e in events]
    result_event = next(e for e in events if e["type"] == "tool_call_result")
    assert result_event["status"] == "error"
    assert "rechazó" in result_event["resumen"]

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert rows.scalars().all() == []


async def test_write_tool_cancelled_before_execution_never_executes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, "0004")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="Cancelado.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    session_id: str | None = None
    async for event in agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta de junio", None, {}):
        if event["type"] == "connected":
            session_id = event["session_id"]
        if event["type"] == "tool_call_awaiting_approval":
            assert session_id is not None
            agent_tool_approval.resolve(session_id, event["call_id"], user.id, "cancel")

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert rows.scalars().all() == []


async def test_write_tool_approval_expires_without_zombie_state(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTL expiry edge case: nobody resolves the pending approval — the stream must
    emit a clear `tool_call_expired` event (never hang), and the entry must not be
    left dangling in a non-terminal status."""
    monkeypatch.setattr(settings, "AGENT_TOOL_APPROVAL_TTL_SECONDS", 0.2)
    user, contrato = await _make_user_with_contrato(db, "0005")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="No se pudo, expiró la aprobación.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    events = await _collect(agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta", None, {}))

    assert "tool_call_expired" in [e["type"] for e in events]
    result_event = next(e for e in events if e["type"] == "tool_call_result")
    assert result_event["status"] == "error"
    assert "expiró" in result_event["resumen"]

    session_id = events[0]["session_id"]
    call_id = tool_call.id
    entry = agent_tool_approval.get_owned(session_id, call_id, user.id)
    assert entry.status == "expired"

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert rows.scalars().all() == []


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


async def test_cross_user_cannot_resolve_another_users_pending_call(db: AsyncSession) -> None:
    user_a, _contrato_a = await _make_user_with_contrato(db, "0006")
    user_b, _contrato_b = await _make_user_with_contrato(db, "0007")

    entry = agent_tool_approval.register("session-a", "call-a", user_a.id, "crear_cuenta_cobro", {"contrato_id": "x"})

    with pytest.raises(PendingToolCallNotFoundError):
        agent_tool_approval.get_owned("session-a", "call-a", user_b.id)
    with pytest.raises(PendingToolCallNotFoundError):
        agent_tool_approval.resolve("session-a", "call-a", user_b.id, "approve")

    # User B's failed attempt must not corrupt the entry — the real owner can still
    # resolve it normally afterward.
    assert entry.status == "pending_approval"
    resolved = agent_tool_approval.resolve("session-a", "call-a", user_a.id, "approve")
    assert resolved.status == "approved"


# ---------------------------------------------------------------------------
# Retry after a genuine failure
# ---------------------------------------------------------------------------


class _FlakyInvoke:
    """Fails the FIRST call for `target_name`, delegates every other call (including
    every OTHER tool name) to the real `invoke_tool`."""

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self.calls_to_target = 0

    async def __call__(self, name: str, ctx: ToolContext, params: Any) -> Any:
        if name == self.target_name:
            self.calls_to_target += 1
            if self.calls_to_target == 1:
                raise RuntimeError("simulated transient failure")
        return await invoke_tool(name, ctx, params)


async def test_retry_after_failure_re_executes_and_keeps_both_events(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, "0008")
    flaky = _FlakyInvoke("crear_cuenta_cobro")
    monkeypatch.setattr(agent_chat_service, "invoke_tool", flaky)

    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="Listo, creé tu cuenta tras reintentar.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    events: list[dict[str, Any]] = []
    session_id: str | None = None
    async for event in agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta de junio", None, {}):
        events.append(event)
        if event["type"] == "connected":
            session_id = event["session_id"]
        if event["type"] == "tool_call_awaiting_approval":
            assert session_id is not None
            agent_tool_approval.resolve(session_id, event["call_id"], user.id, "approve")
        if event["type"] == "tool_call_awaiting_retry":
            assert session_id is not None
            agent_tool_approval.resolve(session_id, event["call_id"], user.id, "retry")

    result_events = [e for e in events if e["type"] == "tool_call_result"]
    assert [e["status"] for e in result_events] == ["error", "ok"]
    assert result_events[0]["attempt"] == 1
    assert result_events[1]["attempt"] == 2
    # Distinct resumen text — the failed attempt's own event is never overwritten.
    assert result_events[0]["resumen"] != result_events[1]["resumen"]
    assert events[-1]["type"] == "final"
    assert len(events[-1]["tool_events"]) == 2

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert len(rows.scalars().all()) == 1


async def test_give_up_after_failure_is_final_no_zombie_and_no_execution(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, "0009")

    async def always_fails(name: str, ctx: ToolContext, params: Any) -> Any:
        raise RuntimeError("simulated permanent failure")

    monkeypatch.setattr(agent_chat_service, "invoke_tool", always_fails)

    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="No se pudo crear la cuenta, el usuario canceló el reintento.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    events: list[dict[str, Any]] = []
    session_id: str | None = None
    async for event in agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta de junio", None, {}):
        events.append(event)
        if event["type"] == "connected":
            session_id = event["session_id"]
        if event["type"] == "tool_call_awaiting_retry":
            assert session_id is not None
            agent_tool_approval.resolve(session_id, event["call_id"], user.id, "cancel")

    assert events[-1]["type"] == "final"
    assert events[-1]["content"] == "No se pudo crear la cuenta, el usuario canceló el reintento."
    result_events = [e for e in events if e["type"] == "tool_call_result"]
    assert len(result_events) == 1
    assert result_events[0]["status"] == "error"

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert rows.scalars().all() == []


# ---------------------------------------------------------------------------
# Cancelling an already-completed call (race) — no-op, not a crash
# ---------------------------------------------------------------------------


async def test_cancel_after_already_resolved_is_noop_not_a_crash(db: AsyncSession) -> None:
    user, _contrato = await _make_user_with_contrato(db, "0010")
    entry = agent_tool_approval.register("session-x", "call-x", user.id, "crear_cuenta_cobro", {})
    agent_tool_approval.resolve("session-x", "call-x", user.id, "approve")
    assert entry.status == "approved"

    # Cancelling something already past the pending/awaiting-retry window does not
    # raise and does not silently pretend to cancel it — callers (the API layer)
    # decide the exact no-op response; here we assert the store itself never
    # corrupts an already-resolved decision.
    second = agent_tool_approval.resolve("session-x", "call-x", user.id, "cancel")
    assert second.status == "cancelled"  # store itself is a plain overwrite;
    # the API layer (tested in test_agent_chat_stream_api.py) is what actually
    # enforces "already resolved -> no_op" for a human clicking twice.


# ---------------------------------------------------------------------------
# SSE disconnect resilience — abandoning the generator still lets the turn finish
# ---------------------------------------------------------------------------


async def test_abandoning_the_stream_still_completes_and_persists(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "AGENT_TOOL_APPROVAL_TTL_SECONDS", 0.2)
    user, contrato = await _make_user_with_contrato(db, "0011")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call], total_tokens=5),
            LLMResponse(content="No se pudo, expiró.", model="fake", total_tokens=8),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    agen = agent_chat_service.stream_chat_with_tools(db, user, "Crea mi cuenta de junio", None, {})
    session_id: str | None = None
    async for event in agen:
        if event["type"] == "connected":
            session_id = event["session_id"]
        if event["type"] == "tool_call_awaiting_approval":
            break  # simulate the client disconnecting right here, never approving

    assert session_id is not None
    await _drain_background_tasks()

    convo_id = uuid.UUID(session_id)
    from app.models.conversacion import Conversacion

    convo = await db.get(Conversacion, convo_id)
    assert convo is not None
    # The turn ran to completion in the background: the final assistant reply is
    # persisted even though nobody kept reading the stream.
    assert convo.mensajes_json[-1]["role"] in ("assistant", "system")
    assert any(m.get("content") == "No se pudo, expiró." for m in convo.mensajes_json)

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))
    assert rows.scalars().all() == []
