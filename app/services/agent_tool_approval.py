"""Standalone in-process state store for interactive write-tool approve/reject/
cancel/retry decisions during the streaming agent chat turn (radicacion-sin-friccion
3.10, `POST /api/v1/agent/chat/stream`).

Problem this solves: a WRITE tool (see `app.tools.registry.ToolSpec.tags`, e.g.
`crear_cuenta_cobro`, `radicar_cuenta`) picked by the LLM inside `chat_with_tools`'s
loop must not execute unattended when the user is watching the docked wizard chat
live — the user needs a chance to approve or reject it, and — if it fails — a chance
to retry the SAME call or give up, all without losing the in-flight SSE connection or
corrupting the turn's state. `app.services.agent_tool_gate.ToolCallGate` is the
async-blocking half of this (awaits an `asyncio.Event`); this module is just the
state it waits on, keyed by `(session_id, call_id)` and scoped to the `usuario_id`
that started the turn.

Storage choice: a plain in-process dict, the SAME pattern already established by
`app.services.evidence_handle_cache` (see that module's docstring for the full
in-process-vs-Redis rationale — it applies unchanged here). The one meaningful
difference from a plain cache: entries here also carry an `asyncio.Event` a
coroutine is actively awaiting, so this state is NOT safely movable to Redis
without also moving the wait itself to a pub/sub or polling loop — noted as a
known limitation of the single-worker deployment this gate assumes, not something
this slice needs to solve.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import settings
from app.core.exceptions import PendingToolCallNotFoundError

# pending_approval  -- write tool picked by the LLM, waiting for approve/reject/cancel
# approved          -- user approved, about to invoke the handler
# running           -- handler invocation in flight (best-effort marker only — see
#                       module docstring: this codebase does not preemptively cancel
#                       an in-flight coroutine, "cancel" while running is a no-op)
# success / error   -- handler finished
# rejected          -- user rejected before execution (tool never invoked)
# cancelled         -- user cancelled (either before execution, or gave up after a
#                       failed attempt instead of retrying)
# expired           -- TTL elapsed with no decision
# awaiting_retry_decision -- handler failed once, waiting for retry-or-cancel
ToolCallStatus = Literal[
    "pending_approval",
    "approved",
    "running",
    "success",
    "error",
    "rejected",
    "cancelled",
    "expired",
    "awaiting_retry_decision",
]

Decision = Literal["approve", "reject", "cancel", "retry"]


@dataclass
class PendingToolCall:
    """One write tool call's interactive control state, live for one chat turn."""

    call_id: str
    session_id: str
    usuario_id: uuid.UUID
    tool_name: str
    args_summary: dict[str, Any]
    status: ToolCallStatus = "pending_approval"
    attempt: int = 1
    decision: Decision | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    expires_at: float = 0.0

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())


_store: dict[str, PendingToolCall] = {}

# Statuses a `PendingToolCall` never transitions OUT of — a decision/execution
# outcome, not an in-flight state. Used by `_sweep_expired_terminal_entries` below
# to decide what is safe to evict.
_TERMINAL_STATUSES: frozenset[ToolCallStatus] = frozenset({"success", "error", "rejected", "cancelled", "expired"})


def _key(session_id: str, call_id: str) -> str:
    return f"{session_id}:{call_id}"


def _sweep_expired_terminal_entries() -> None:
    """Evict every entry that is BOTH terminal AND past its TTL window (BLOCKER 2,
    phase3-agent-sse-approval-gate adversarial review).

    Without this, terminal entries accumulate in `_store` forever — `discard()`
    below was dead code, never called anywhere in `app/` — each still holding the
    FULL normalized tool-call args (potentially free-text/PII) for the life of the
    process. Mirrors the lazy-eviction principle `app.services.evidence_handle_cache.
    redeem()` already applies on every access to THAT store; this one has no single
    per-entry accessor called for every live entry, so eviction is instead swept
    from `register()` — a frequent, cheap entry point every new write-tool call
    already goes through.

    Deliberately terminal-only: a LIVE entry (`pending_approval`, `approved`,
    `running`, `awaiting_retry_decision`) needs its OWN in-flight `ToolCallGate`
    coroutine to transition it — evicting it here, even past `expires_at`, would
    strand that coroutine's later `resolve()`/`get_owned()` lookups. And a terminal
    entry still WITHIN its TTL window is kept on purpose: `get_owned`'s own
    docstring explains why a terminal-but-not-yet-swept entry should resolve a
    racing control-endpoint call (e.g. a "cancel" click landing right as the call
    finishes) to a graceful no-op response, not a bare 404 — this sweep only ever
    removes an entry once nothing legitimate should still be asking about it.
    """
    now = time.monotonic()
    stale = [
        (entry.session_id, entry.call_id)
        for entry in _store.values()
        if entry.status in _TERMINAL_STATUSES and now >= entry.expires_at
    ]
    for session_id, call_id in stale:
        discard(session_id, call_id)


def register(
    session_id: str,
    call_id: str,
    usuario_id: uuid.UUID,
    tool_name: str,
    args_summary: dict[str, Any],
) -> PendingToolCall:
    """Create (or reset) the pending entry for one write tool call, status=pending_approval.

    Also sweeps expired terminal entries out of the store first (see
    `_sweep_expired_terminal_entries`) — bounding the store's lifetime memory
    footprint regardless of how many prior calls' entries were never explicitly
    discarded.
    """
    _sweep_expired_terminal_entries()
    entry = PendingToolCall(
        call_id=call_id,
        session_id=session_id,
        usuario_id=usuario_id,
        tool_name=tool_name,
        args_summary=args_summary,
        expires_at=time.monotonic() + settings.AGENT_TOOL_APPROVAL_TTL_SECONDS,
    )
    _store[_key(session_id, call_id)] = entry
    return entry


def reopen_for_retry_decision(session_id: str, call_id: str) -> PendingToolCall | None:
    """After a failed attempt, reopen the SAME entry for a retry-or-cancel decision.

    Returns `None` if the entry vanished (should not normally happen — the caller
    only calls this right after it registered/ran the same call_id). A fresh
    `asyncio.Event` and TTL window are issued so a stale `.wait()` from an earlier
    decision round can never resolve this new one.
    """
    entry = _store.get(_key(session_id, call_id))
    if entry is None:
        return None
    entry.status = "awaiting_retry_decision"
    entry.decision = None
    entry.event = asyncio.Event()
    entry.expires_at = time.monotonic() + settings.AGENT_TOOL_APPROVAL_TTL_SECONDS
    return entry


def get_owned(session_id: str, call_id: str, usuario_id: uuid.UUID) -> PendingToolCall:
    """Return the pending entry for `(session_id, call_id)`, scoped to `usuario_id`.

    Raises `PendingToolCallNotFoundError` for every failure mode (unknown call_id,
    owned by a different user) — see that exception's docstring for why these are
    deliberately not distinguished. Does NOT check expiry here: an expired entry is
    still a real record the owner should be able to see the terminal status of
    (`status="expired"`) rather than getting a bare 404 — `ToolCallGate` itself
    transitions status to `"expired"` when its own wait times out.
    """
    entry = _store.get(_key(session_id, call_id))
    if entry is None or entry.usuario_id != usuario_id:
        raise PendingToolCallNotFoundError()
    return entry


# The status a decision implies IMMEDIATELY, set synchronously inside `resolve()`
# itself rather than left for whichever `ToolCallGate` coroutine wakes up next to
# assign later. Without this, a control endpoint reading `entry.status` right after
# calling `resolve()` would race the gate's own wakeup — two rapid clicks on
# "reject" could both see `status="pending_approval"` and both report `action=
# "rejected"` instead of the second one correctly reporting a no-op. `resolve()`
# being the single, synchronous authority over the DECISION-driven part of the
# status makes every control endpoint response consistent regardless of whether a
# live `ToolCallGate` is even still waiting (e.g. after an SSE disconnect — see
# `app.services.agent_chat_service.stream_chat_with_tools`'s docstring). The gate
# still owns the LATER, execution-driven transitions (`"approved" -> "running" ->
# "success"/"error"`), which a decision alone can't determine.
_DECISION_STATUS: dict[Decision, ToolCallStatus] = {
    "approve": "approved",
    "reject": "rejected",
    "cancel": "cancelled",
    "retry": "approved",  # about to re-execute — the gate advances this to "running"
}


def resolve(
    session_id: str,
    call_id: str,
    usuario_id: uuid.UUID,
    decision: Decision,
    *,
    expected: tuple[ToolCallStatus, ...] | None = None,
) -> tuple[bool, PendingToolCall]:
    """Record the user's decision, set the status it implies, and wake up whichever
    `ToolCallGate` coroutine is awaiting this entry's event. Ownership-checked the
    same way as `get_owned`.

    WARNING fix (phase3-agent-sse-approval-gate adversarial review): the
    allowed-status CHECK used to live in each of the 4 control endpoints (`if
    entry.status != "pending_approval": ... else: resolve(...)`) — two separate
    statements that happened to be race-free today only because `resolve()` itself
    has no `await` inside it (the GIL makes each of the two calls individually
    atomic), not because the pair of them was designed to be atomic together. A
    service-layer `asyncio.gather(approve(X), reject(X))` proved this: without a
    single atomic check-and-set, "last writer wins" (both mutate) instead of
    exactly one decision winning.

    Pass `expected` — the tuple of statuses this decision is only valid to apply
    FROM — and this function makes the check-and-set ONE synchronous unit: when
    `entry.status` is not in `expected`, it returns `(False, entry)` WITHOUT
    mutating anything, so a caller that only acts on `changed=True` can never
    observe a state that changed out from under it between checking and acting.
    `expected=None` (the default) skips the check entirely — unconditional
    overwrite, the exact behavior this function had before this fix — for callers
    (mostly tests exercising the gate/store directly) that intentionally don't
    gate on a prior status.
    """
    entry = get_owned(session_id, call_id, usuario_id)
    if expected is not None and entry.status not in expected:
        return False, entry
    entry.decision = decision
    entry.status = _DECISION_STATUS[decision]
    entry.event.set()
    return True, entry


def discard(session_id: str, call_id: str) -> None:
    _store.pop(_key(session_id, call_id), None)


def clear() -> None:
    """Test-only helper: wipe the whole store."""
    _store.clear()
