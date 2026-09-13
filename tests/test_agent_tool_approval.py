"""Unit tests for `app.services.agent_tool_approval` — the in-process store behind
the streaming agent chat's write-tool approve/reject/cancel/retry gate
(radicacion-sin-friccion 3.10, `POST /api/v1/agent/chat/stream`).

No DB dependency: this module is a plain in-process dict, so these tests run
without the `db` fixture.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from app.services import agent_tool_approval

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_store() -> Any:
    agent_tool_approval.clear()
    yield
    agent_tool_approval.clear()


# ---------------------------------------------------------------------------
# BLOCKER 2 (phase3-agent-sse-approval-gate adversarial review): unbounded
# memory growth — terminal entries never evicted.
# ---------------------------------------------------------------------------


async def test_registering_a_new_call_sweeps_expired_terminal_entries() -> None:
    """Mirrors the reviewer's own probe: 50 terminal entries past TTL must be
    evicted by the time the store is touched again, not accumulate forever.

    Seeds `_store` directly (not via `register()`, which itself sweeps on every
    call — going through it here would sweep incrementally as the loop runs
    instead of letting all 50 accumulate first, the exact scenario being probed).
    """
    usuario_id = uuid.uuid4()
    session_id = "session-sweep"

    for i in range(50):
        entry = agent_tool_approval.PendingToolCall(
            call_id=f"call-{i}",
            session_id=session_id,
            usuario_id=usuario_id,
            tool_name="crear_cuenta_cobro",
            args_summary={},
            status="success",
            expires_at=time.monotonic() - 1,  # already past its TTL window
        )
        agent_tool_approval._store[agent_tool_approval._key(session_id, f"call-{i}")] = entry

    assert len(agent_tool_approval._store) == 50

    agent_tool_approval.register(session_id, "call-new", usuario_id, "crear_cuenta_cobro", {})

    assert len(agent_tool_approval._store) == 1
    remaining = agent_tool_approval._store[agent_tool_approval._key(session_id, "call-new")]
    assert remaining.status == "pending_approval"


async def test_sweep_calls_discard_not_just_a_duplicate_pop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`discard()` must actually be the code path the sweep uses — it was dead code
    (never called anywhere in `app/`) before this fix."""
    usuario_id = uuid.uuid4()
    session_id = "session-discard"
    entry = agent_tool_approval.register(session_id, "call-old", usuario_id, "crear_cuenta_cobro", {})
    entry.status = "error"
    entry.expires_at = time.monotonic() - 1

    calls: list[tuple[str, str]] = []
    original_discard = agent_tool_approval.discard

    def _spy(session_id: str, call_id: str) -> None:
        calls.append((session_id, call_id))
        original_discard(session_id, call_id)

    monkeypatch.setattr(agent_tool_approval, "discard", _spy)

    agent_tool_approval.register(session_id, "call-trigger", usuario_id, "crear_cuenta_cobro", {})

    assert (session_id, "call-old") in calls


async def test_sweep_never_evicts_a_live_pending_entry_even_past_its_ttl() -> None:
    """A live (non-terminal) entry must survive the sweep regardless of TTL — only
    its OWN `ToolCallGate` coroutine (via `resolve()` or its own timeout) may
    transition it; sweeping it out here would strand that coroutine's `resolve()`/
    `get_owned()` lookups."""
    usuario_id = uuid.uuid4()
    session_id = "session-live"
    agent_tool_approval.register(session_id, "call-live", usuario_id, "crear_cuenta_cobro", {})
    live_entry = agent_tool_approval._store[agent_tool_approval._key(session_id, "call-live")]
    live_entry.expires_at = time.monotonic() - 1  # simulate a stale-but-still-pending entry

    agent_tool_approval.register(session_id, "call-trigger-sweep", usuario_id, "crear_cuenta_cobro", {})

    assert agent_tool_approval._store.get(agent_tool_approval._key(session_id, "call-live")) is not None


async def test_sweep_never_evicts_a_terminal_entry_still_within_its_ttl_grace_window() -> None:
    """A terminal entry within its TTL window stays visible so a racing control
    endpoint call still gets a graceful no-op response, not a bare 404 (see
    `get_owned`'s docstring on why an expired-but-not-yet-swept entry stays a real,
    visible record)."""
    usuario_id = uuid.uuid4()
    session_id = "session-grace"
    entry = agent_tool_approval.register(session_id, "call-grace", usuario_id, "crear_cuenta_cobro", {})
    entry.status = "success"
    # expires_at left in the future (register()'s default) — still within the grace window.

    agent_tool_approval.register(session_id, "call-trigger-sweep-2", usuario_id, "crear_cuenta_cobro", {})

    assert agent_tool_approval._store.get(agent_tool_approval._key(session_id, "call-grace")) is not None
