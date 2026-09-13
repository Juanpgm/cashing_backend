"""Streaming counterpart to `agent_chat.py` — docks the free-form tool-calling agent
into the radicación wizard (radicacion-sin-friccion 3.10).

`POST /chat/stream` is a NEW endpoint (not an `Accept: text/event-stream` branch on
the existing `POST /chat`) for three concrete reasons, following this codebase's own
established SSE precedent at `app/api/v1/agent_sessions.py::stream_agent_progress`
(same router, same `_sse_event` framing, same `text/event-stream` + no-cache headers):

1. `POST /chat` is `multipart/form-data` (message + optional file uploads) with a
   `response_model=AgentChatResult` — FastAPI ties both the request body parser and
   the OpenAPI response schema to ONE endpoint signature. A response-shape branch
   inside the same handler would either lie in the OpenAPI schema (still declaring
   `AgentChatResult` while actually streaming) or require dropping typed response
   docs for both callers.
2. `agent_sessions.py` already establishes "new endpoint + `_sse_event` helper" as
   this codebase's SSE convention — branching an existing endpoint on `Accept` has
   no precedent here.
3. Every consumer of `POST /chat` (the standalone `app/agent/page.tsx` today) keeps
   working with ZERO changes: it never has to add an `Accept` header or otherwise
   opt out of a new default behavior.

Event vocabulary yielded by `POST /chat/stream` (each SSE `data:` line is exactly
one JSON object with a `"type"` key):

- `connected`            {session_id}
- `tool_call_started`    {call_id, tool, write, attempt}
- `tool_call_awaiting_approval`  {call_id, tool}  (write tools only, before 1st attempt)
- `tool_call_approved` / `tool_call_rejected` / `tool_call_cancelled` / `tool_call_expired`
                         {call_id, tool}          (approval outcome, write tools only)
- `tool_call_awaiting_retry`     {call_id, tool, attempt}  (after a write tool's
                         attempt failed, before the loop accepts it as final)
- `tool_call_retry_retry` / `tool_call_retry_cancelled` / `tool_call_retry_expired`
                         {call_id, tool}          (retry-decision outcome)
- `tool_call_result`     {call_id, tool, status, resumen, duration_ms, attempt}
                         (mirrors one `ToolEvent` — read tools go straight to this,
                         no approval events in between)
- `final`                {session_id, content, tool_events, documentos, tokens_used,
                         ui_actions}              (same shape as `AgentChatResult`,
                         flattened — always the last event on success)
- `error`                {detail}                 (turn could not complete at all)

See `app.services.agent_chat_service.stream_chat_with_tools` for the producer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.api.v1.agent_chat import parse_chat_attachments
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.schemas.agent import ToolCallControlResponse
from app.services import agent_chat_service, agent_tool_approval

logger = structlog.get_logger("api.agent_chat_stream")

router = APIRouter(prefix="/agent", tags=["agent"])


def _sse_event(data: dict[str, Any]) -> str:
    """Format a dict as an SSE data line — same framing as `agent_sessions._sse_event`."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@router.post("/chat/stream", status_code=200)
@limiter.limit("5/minute")
async def chat_stream(
    request: Request,
    user: CurrentUser,
    message: str = Form(..., min_length=1, max_length=5000),
    session_id: str | None = Form(None),
    contrato_id: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Server-Sent Events counterpart to `POST /chat` — see module docstring for the
    full event vocabulary. Same request shape (multipart form) and the same file
    count/size/type limits as `POST /chat` (`parse_chat_attachments`).

    Every WRITE tool (`"write" in ToolSpec.tags` — `crear_cuenta_cobro`,
    `radicar_cuenta`, `marcar_requisito`, `persistir_evidencias`, `editar_actividad`,
    etc.) the agent decides to call pauses the stream and emits
    `tool_call_awaiting_approval` until the caller (or another authenticated request
    on the same session) resolves it via one of the `tool-calls/{call_id}/...`
    endpoints below, or its approval window elapses (`tool_call_expired`). Read-only
    tools (`listar_contratos`, `resumen_checklist`, `obtener_estado_radicacion`, ...)
    execute immediately with no pause, identically to `POST /chat`.
    """
    attachments = await parse_chat_attachments(files)

    async def event_stream() -> AsyncIterator[str]:
        async for event in agent_chat_service.stream_chat_with_tools(
            db=db,
            usuario=user,
            message=message,
            session_id=session_id,
            attachments=attachments,
            contrato_id=contrato_id,
        ):
            yield _sse_event({**event, "timestamp": _now_iso()})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _control_response(entry: agent_tool_approval.PendingToolCall, action: str, message: str) -> ToolCallControlResponse:
    return ToolCallControlResponse(
        call_id=entry.call_id, tool=entry.tool_name, status=entry.status, action=action, message=message
    )


@router.post("/chat/stream/{session_id}/tool-calls/{call_id}/approve", response_model=ToolCallControlResponse)
async def approve_tool_call(session_id: str, call_id: str, user: CurrentUser) -> ToolCallControlResponse:
    """Approve a pending WRITE tool call so the stream can execute it.

    404s (`PendingToolCallNotFoundError`, never a leak — see that exception's
    docstring) for an unknown `call_id`, an expired entry that was already garbage,
    or a `call_id` that belongs to a DIFFERENT user's session — approving/rejecting/
    cancelling/retrying another user's pending tool call is never possible.
    """
    changed, entry = agent_tool_approval.resolve(
        session_id, call_id, user.id, "approve", expected=("pending_approval",)
    )
    if not changed:
        message = f"Esta acción ya no está pendiente de aprobación (estado: {entry.status})."
        return _control_response(entry, "no_op", message)
    return _control_response(entry, "approved", "Aprobado.")


@router.post("/chat/stream/{session_id}/tool-calls/{call_id}/reject", response_model=ToolCallControlResponse)
async def reject_tool_call(session_id: str, call_id: str, user: CurrentUser) -> ToolCallControlResponse:
    """Reject a pending WRITE tool call — it will NOT execute. See `approve_tool_call`
    for the ownership/404 contract, identical here."""
    changed, entry = agent_tool_approval.resolve(session_id, call_id, user.id, "reject", expected=("pending_approval",))
    if not changed:
        message = f"Esta acción ya no está pendiente de aprobación (estado: {entry.status})."
        return _control_response(entry, "no_op", message)
    return _control_response(entry, "rejected", "Rechazado.")


@router.post("/chat/stream/{session_id}/tool-calls/{call_id}/cancel", response_model=ToolCallControlResponse)
async def cancel_tool_call(session_id: str, call_id: str, user: CurrentUser) -> ToolCallControlResponse:
    """Cancel a pending WRITE tool call (before execution) or give up on retrying one
    that already failed. A call that is already `running` (best-effort marker only —
    this codebase does not preemptively interrupt an in-flight handler coroutine) or
    already in a terminal state (`success`/`error`/`rejected`/`cancelled`/`expired`)
    returns a clear no-op instead of an error or a double-execution."""
    changed, entry = agent_tool_approval.resolve(
        session_id, call_id, user.id, "cancel", expected=("pending_approval", "awaiting_retry_decision")
    )
    if not changed:
        message = f"Esta acción ya no se puede cancelar (estado: {entry.status})."
        return _control_response(entry, "no_op", message)
    return _control_response(entry, "cancelled", "Cancelado.")


@router.post("/chat/stream/{session_id}/tool-calls/{call_id}/retry", response_model=ToolCallControlResponse)
async def retry_tool_call(session_id: str, call_id: str, user: CurrentUser) -> ToolCallControlResponse:
    """Retry a WRITE tool call that just failed and is awaiting a retry-or-cancel
    decision. Any other status (still awaiting approval, already succeeded, already
    terminal) returns a clear no-op — retry is only meaningful after a genuine
    failure, never a way to re-run a call that hasn't failed."""
    changed, entry = agent_tool_approval.resolve(
        session_id, call_id, user.id, "retry", expected=("awaiting_retry_decision",)
    )
    if not changed:
        message = f"Esta acción no está esperando una decisión de reintento (estado: {entry.status})."
        return _control_response(entry, "no_op", message)
    return _control_response(entry, "retry_requested", "Reintento solicitado.")
