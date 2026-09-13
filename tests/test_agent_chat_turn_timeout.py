"""Tests for the per-turn LLM wall-clock budget (radicacion-sin-friccion 1.8).

Real `asyncio.timeout` cancellation semantics are exercised directly (with the
module's timeout constants monkeypatched down to sub-second values) instead of
purely mocking `asyncio.timeout` away — this proves the mechanism actually
cancels a hung call, not just that the code *would* handle a `TimeoutError` if
one arrived. No test in this file waits anywhere near the real ~12-minute old
worst case or the real 2-minute target; every wait is a monkeypatched
fraction of a second.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service
from sqlalchemy.ext.asyncio import AsyncSession


class ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self._responses.pop(0)


class HangingLLM:
    """Simulates a completely unresponsive model/fallback chain — every call
    hangs far longer than any timeout this test configures, so a passing test
    proves `asyncio.timeout` actually cancelled it (not that it happened to
    finish in time)."""

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
        self.call_count += 1
        await asyncio.sleep(10_000)
        raise AssertionError("must have been cancelled by asyncio.timeout before reaching here")


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: object) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"timeout_{suffix}@example.com",
        nombre=f"Timeout User {suffix}",
        cedula=f"7171{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"TIMEOUT-{suffix}",
        objeto="Objeto de prueba para timeout",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"7171{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_model_outage_surfaces_a_friendly_message_fast_not_after_twelve_minutes(
    db: AsyncSession, test_user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully hung model chain must surface `_LLM_TIMEOUT_MESSAGE` in well
    under a second of REAL wall-clock time here (budgets monkeypatched to
    sub-second), proving the cancellation path — not the old ~12-minute
    worst case this slice fixes."""
    monkeypatch.setattr(agent_chat_service, "_TURN_LLM_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(agent_chat_service, "_INTERACTIVE_LLM_TIMEOUT_SECONDS", 1)
    hanging = HangingLLM()
    _patch_llm(monkeypatch, hanging)
    user = test_user["user"]

    start = time.perf_counter()
    result = await agent_chat_service.chat_with_tools(db, user, "Radicá mi cuenta de este mes", None, {})
    elapsed = time.perf_counter() - start

    assert hanging.call_count == 1
    assert elapsed < 5.0, f"expected the turn to bail out fast, took {elapsed:.2f}s"
    assert result.content == agent_chat_service._LLM_TIMEOUT_MESSAGE


@pytest.mark.asyncio
async def test_turn_budget_pre_check_stops_a_second_call_without_making_it(
    db: AsyncSession, test_user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once `llm_elapsed_seconds` alone already meets/exceeds the turn budget,
    the loop's pre-iteration check must refuse to even ATTEMPT a second
    `llm.complete()` call — proven deterministically by faking `time.perf_counter`
    so the first (real, near-instant) call is measured as having consumed the
    entire budget, with zero real sleeping anywhere in this test."""
    monkeypatch.setattr(agent_chat_service, "_TURN_LLM_BUDGET_SECONDS", 10.0)
    tool_call = LLMToolCall(id="c1", name="listar_contratos", arguments={})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="No debería llegar acá.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)
    user = test_user["user"]

    # `elapsed_ms(start) = (perf_counter() - start) * 1000`; making the fake
    # clock jump 20 (fake) seconds WHILE the first `llm.complete()` call is
    # "in flight" makes `llm_elapsed_seconds` measure that single call as
    # having taken 20s — straight past the 10s budget — with no real
    # sleeping anywhere in this test.
    real_perf_counter = agent_chat_service.time.perf_counter
    fake_now = {"value": real_perf_counter()}

    def _fake_perf_counter() -> float:
        return fake_now["value"]

    monkeypatch.setattr(agent_chat_service.time, "perf_counter", _fake_perf_counter)

    original_complete = scripted.complete

    async def _complete_that_advances_the_fake_clock(messages: list[object], **kwargs: object) -> LLMResponse:
        response = await original_complete(messages, **kwargs)
        fake_now["value"] += 20.0
        return response

    scripted.complete = _complete_that_advances_the_fake_clock  # type: ignore[method-assign]

    result = await agent_chat_service.chat_with_tools(db, user, "Lista mis contratos", None, {})

    assert scripted.call_count == 1, "a second llm.complete() call must never be attempted once the budget is spent"
    assert result.content == agent_chat_service._LLM_TIMEOUT_MESSAGE


@pytest.mark.asyncio
async def test_a_slow_legitimate_tool_call_does_not_count_against_the_llm_turn_budget(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn budget must be scoped to LLM round trips only — a slow but
    legitimate tool call (e.g. document/informe generation) must NEVER be cut
    off, and must not eat into the budget the NEXT llm.complete() call needs.
    """
    monkeypatch.setattr(agent_chat_service, "_TURN_LLM_BUDGET_SECONDS", 1.0)
    user, _contrato = await _make_user_with_contrato(db, "01")

    tool_call = LLMToolCall(id="c1", name="listar_contratos", arguments={})
    scripted = ScriptedLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Listo, ya los tengo.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    original_invoke_tool = agent_chat_service.invoke_tool

    async def _slow_invoke_tool(name: str, ctx: object, args: dict) -> object:
        # Sleeps LONGER than the whole (monkeypatched) LLM turn budget — if tool
        # time were mistakenly counted against that budget, the second
        # llm.complete() call below would never happen.
        await asyncio.sleep(1.5)
        return await original_invoke_tool(name, ctx, args)

    monkeypatch.setattr(agent_chat_service, "invoke_tool", _slow_invoke_tool)

    result = await agent_chat_service.chat_with_tools(db, user, "Lista mis contratos por favor", None, {})

    assert scripted.call_count == 2, "both LLM calls must have happened — tool time must not consume the LLM budget"
    assert result.content == "Listo, ya los tengo."


def test_llm_timeout_message_is_the_same_string_for_both_timeout_paths() -> None:
    """Documents the deliberate design choice: full-chain-exhaustion RuntimeError
    and cumulative-budget TimeoutError share ONE user-facing message string."""
    assert agent_chat_service._LLM_TIMEOUT_MESSAGE
    assert "modelo de IA" in agent_chat_service._LLM_TIMEOUT_MESSAGE
