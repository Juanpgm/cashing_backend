"""Async-blocking half of the write-tool approval gate (radicacion-sin-friccion 3.10).

`ToolCallGate` is the object `agent_chat_service._run_chat_turn` awaits, when the
streaming endpoint supplies one, right before executing a WRITE tool and again right
after a WRITE tool's attempt fails. It registers/updates state in
`app.services.agent_tool_approval` and blocks on that entry's `asyncio.Event` until
one of the `POST /api/v1/agent/chat/stream/{session_id}/tool-calls/{call_id}/...`
control endpoints resolves it, or its TTL elapses.

Not used at all by the existing synchronous `POST /api/v1/agent/chat` endpoint —
`_run_chat_turn`'s `approval_gate` parameter defaults to `None`, and every write tool
call runs immediately exactly as it did before this slice.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from app.services import agent_tool_approval as store

logger = structlog.get_logger("services.agent_tool_gate")

ApprovalOutcome = Literal["approved", "rejected", "cancelled", "expired"]
RetryOutcome = Literal["retry", "cancelled", "expired"]


@dataclass
class ToolCallGate:
    """One instance per streamed chat turn, scoped to the session and its user."""

    session_id: str
    usuario_id: uuid.UUID

    async def request_approval(self, call_id: str, tool_name: str, args_summary: dict[str, Any]) -> ApprovalOutcome:
        """Register `call_id` as pending and block until approve/reject/cancel/TTL.

        Status for "rejected"/"cancelled" is already set by `agent_tool_approval.
        resolve()` at decision time (synchronous, no race) — this only advances
        "approved" the rest of the way to "running" right before actually invoking
        the tool, and owns the "expired" transition on its own timeout.
        """
        entry = store.register(self.session_id, call_id, self.usuario_id, tool_name, args_summary)
        outcome = await self._await_decision(entry)
        if outcome == "expired":
            entry.status = "expired"
            await logger.awarning("agent_tool_approval_expired", call_id=call_id, tool=tool_name)
            return "expired"
        if entry.decision == "approve":
            entry.status = "running"
            return "approved"
        if entry.decision == "reject":
            return "rejected"
        # decision == "cancel" (or, defensively, anything else the event woke up for)
        return "cancelled"

    async def request_retry_decision(self, call_id: str) -> RetryOutcome:
        """Reopen `call_id` (already registered — see `request_approval`) after a
        failed attempt and block until retry/cancel/TTL. See `request_approval` for
        why only "retry"/"expired" set status here — "cancelled" is already set by
        `resolve()`."""
        entry = store.reopen_for_retry_decision(self.session_id, call_id)
        if entry is None:
            # Should not happen in practice (retry decision is only requested for a
            # call_id this same gate just registered), but fail safe rather than
            # raise into the middle of the tool-call loop.
            return "cancelled"
        outcome = await self._await_decision(entry)
        if outcome == "expired":
            entry.status = "expired"
            await logger.awarning("agent_tool_retry_expired", call_id=call_id, tool=entry.tool_name)
            return "expired"
        if entry.decision == "retry":
            entry.attempt += 1
            entry.status = "running"
            return "retry"
        return "cancelled"

    @staticmethod
    async def _await_decision(entry: store.PendingToolCall) -> Literal["decided", "expired"]:
        timeout = max(0.0, entry.expires_at - time.monotonic())
        try:
            await asyncio.wait_for(entry.event.wait(), timeout=timeout)
        except TimeoutError:
            return "expired"
        return "decided"
