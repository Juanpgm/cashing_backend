"""Agent Sessions API — SSE progress streaming and HIL feedback endpoints (Phase 6)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.models.agent_run import AgentRun
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.conversacion import Conversacion
from app.services import agent_service

logger = structlog.get_logger("api.agent_sessions")

router = APIRouter(prefix="/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """User feedback to resume a paused HIL agent run."""

    feedback: str
    action: str = "continue"  # "continue" | "abort" | "retry"
    metadata: dict[str, Any] | None = None


class FeedbackResponse(BaseModel):
    """Acknowledgment of submitted feedback."""

    session_id: uuid.UUID
    borrador_version: int | None = None
    status: str  # "resumed" | "aborted"
    message: str


class AgentRunSummary(BaseModel):
    """Summary of an agent run."""

    id: uuid.UUID
    modo: str
    estado: str
    nodo_actual: str | None
    quality_score: float | None
    tokens_usados: int | None
    costo_usd: float | None
    created_at: datetime
    completed_at: datetime | None


# ---------------------------------------------------------------------------
# SSE streaming endpoint
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/stream")
async def stream_agent_progress(
    session_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Server-Sent Events stream of agent node progress for a session.

    Clients connect to this endpoint while the agent is running.
    Each SSE event is a JSON object:

    ```json
    { "type": "node_start"|"node_done"|"hil_pause"|"completed"|"error",
      "node": "extraction_node",
      "phase": "obligations_extraction",
      "timestamp": "2026-01-01T12:00:00Z" }
    ```

    The stream ends with a ``{"type": "completed"}`` or ``{"type": "error"}`` event.
    """
    # Verify the session belongs to the authenticated user
    conv_result = await db.execute(
        select(Conversacion).where(
            Conversacion.id == session_id,
            Conversacion.usuario_id == user.id,
        )
    )
    conversacion = conv_result.scalar_one_or_none()
    if conversacion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or does not belong to you.",
        )

    # Fetch the latest agent_run for this session
    run_result = await db.execute(
        select(AgentRun)
        .where(AgentRun.conversacion_id == session_id, AgentRun.usuario_id == user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    agent_run = run_result.scalar_one_or_none()

    async def event_stream() -> AsyncIterator[str]:
        """Poll agent_run for state changes and emit SSE events."""
        run_id = agent_run.id if agent_run else None
        last_nodo: str | None = None
        ticks = 0
        max_ticks = 120  # max 2 minutes of polling

        # Send initial connection event
        yield _sse_event(
            {
                "type": "connected",
                "session_id": str(session_id),
                "run_id": str(run_id) if run_id else None,
                "timestamp": _now_iso(),
            }
        )

        if run_id is None:
            yield _sse_event({"type": "error", "detail": "No active run for session"})
            return

        while ticks < max_ticks:
            await asyncio.sleep(1)
            ticks += 1

            # Re-fetch agent run to detect state changes
            async with db.begin_nested():
                fresh = await db.get(AgentRun, run_id)

            if fresh is None:
                yield _sse_event({"type": "error", "detail": "Run not found"})
                return

            current_nodo = fresh.nodo_actual
            if current_nodo != last_nodo and current_nodo:
                yield _sse_event(
                    {
                        "type": "node_progress",
                        "node": current_nodo,
                        "estado": fresh.estado,
                        "timestamp": _now_iso(),
                    }
                )
                last_nodo = current_nodo

            if fresh.estado == "pausado_hil":
                yield _sse_event(
                    {
                        "type": "hil_pause",
                        "node": current_nodo,
                        "message": "El agente necesita tu aprobación para continuar.",
                        "timestamp": _now_iso(),
                    }
                )
                return  # client will reconnect after HIL

            if fresh.estado in ("completado", "fallido"):
                yield _sse_event(
                    {
                        "type": "completed" if fresh.estado == "completado" else "error",
                        "estado": fresh.estado,
                        "quality_score": float(fresh.quality_score) if fresh.quality_score else None,
                        "tokens_usados": fresh.tokens_usados,
                        "costo_usd": float(fresh.costo_usd) if fresh.costo_usd else None,
                        "timestamp": _now_iso(),
                    }
                )
                return

        # Timeout
        yield _sse_event(
            {
                "type": "timeout",
                "message": "Stream timeout — reconnect to continue monitoring.",
                "timestamp": _now_iso(),
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# HIL feedback endpoint
# ---------------------------------------------------------------------------


@router.patch("/sessions/{session_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    session_id: uuid.UUID,
    body: FeedbackRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FeedbackResponse:
    """Submit user feedback to resume a paused HIL agent session.

    When the agent pauses for human review (``estado = "pausado_hil"``), the
    frontend sends this endpoint with the user's decision.  The feedback is
    stored in the latest ``BorradorCuentaCobro`` and the run is marked for
    resumption.

    Actions:
    - ``continue``: Persist feedback → next agent invocation will use it.
    - ``abort``: Mark run as ``fallido`` and do not resume.
    - ``retry``: Reset run to ``en_progreso`` for retry.
    """
    # Verify session ownership
    conv_result = await db.execute(
        select(Conversacion).where(
            Conversacion.id == session_id,
            Conversacion.usuario_id == user.id,
        )
    )
    if conv_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )

    # Fetch the latest agent_run
    run_result = await db.execute(
        select(AgentRun)
        .where(AgentRun.conversacion_id == session_id, AgentRun.usuario_id == user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    agent_run = run_result.scalar_one_or_none()
    if agent_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No agent run found for this session.",
        )

    # Fetch latest borrador and persist feedback
    borrador_result = await db.execute(
        select(BorradorCuentaCobro)
        .join(
            Conversacion,
            BorradorCuentaCobro.cuenta_cobro_id == Conversacion.id,
            isouter=True,
        )
        .order_by(BorradorCuentaCobro.created_at.desc())
        .limit(1)
    )
    borrador = borrador_result.scalar_one_or_none()
    borrador_version: int | None = None

    if borrador is not None:
        borrador.feedback_usuario = body.feedback
        borrador_version = borrador.version
        db.add(borrador)

    # Update agent_run state based on action
    if body.action == "abort":
        agent_run.estado = "fallido"
        agent_run.error_mensaje = f"Abortado por usuario: {body.feedback[:200]}"
        new_status = "aborted"
        message = "Ejecución abortada."
        db.add(agent_run)
        await db.commit()
    elif body.action == "retry":
        agent_run.estado = "en_progreso"
        new_status = "resumed"
        message = "Reintento solicitado. El agente retomará desde el inicio."
        db.add(agent_run)
        await db.commit()
    else:
        # continue — resume graph from checkpoint with user feedback
        try:
            await agent_service.resume(db, session_id, body.feedback)
            new_status = "resumed"
            message = "Feedback recibido. El agente ha reanudado la ejecución."
        except Exception as exc:
            # Checkpoint not found or graph error — fall back to in-progress marker
            await logger.awarning("hil_resume_failed", error=str(exc), session_id=str(session_id))
            agent_run.estado = "en_progreso"
            agent_run.nodo_actual = None
            db.add(agent_run)
            new_status = "resumed"
            message = "Feedback recibido. El agente reanudará desde el último checkpoint."
            await db.commit()

    await logger.ainfo(
        "hil_feedback_submitted",
        session_id=str(session_id),
        action=body.action,
        borrador_version=borrador_version,
    )

    return FeedbackResponse(
        session_id=session_id,
        borrador_version=borrador_version,
        status=new_status,
        message=message,
    )


# ---------------------------------------------------------------------------
# Agent run history
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/runs", response_model=list[AgentRunSummary])
async def list_session_runs(
    session_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AgentRunSummary]:
    """List all agent runs for a session, newest first."""
    conv_result = await db.execute(
        select(Conversacion).where(
            Conversacion.id == session_id,
            Conversacion.usuario_id == user.id,
        )
    )
    if conv_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    result = await db.execute(
        select(AgentRun)
        .where(AgentRun.conversacion_id == session_id)
        .order_by(AgentRun.created_at.desc())
        .limit(50)
    )
    runs = result.scalars().all()
    return [
        AgentRunSummary(
            id=r.id,
            modo=r.modo,
            estado=r.estado,
            nodo_actual=r.nodo_actual,
            quality_score=float(r.quality_score) if r.quality_score else None,
            tokens_usados=r.tokens_usados,
            costo_usd=float(r.costo_usd) if r.costo_usd else None,
            created_at=r.created_at,
            completed_at=r.completed_at,
        )
        for r in runs
    ]


# ---------------------------------------------------------------------------
# Admin endpoint — all runs across all users
# ---------------------------------------------------------------------------


@router.get("/admin/runs", response_model=list[AgentRunSummary], tags=["admin"])
async def admin_list_runs(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRunSummary]:
    """Return all agent runs across all users (admin only).

    Requires the authenticated user to have ``is_superuser=True``.
    """
    if not getattr(user, "is_superuser", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")

    result = await db.execute(
        select(AgentRun)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = result.scalars().all()
    return [
        AgentRunSummary(
            id=r.id,
            modo=r.modo,
            estado=r.estado,
            nodo_actual=r.nodo_actual,
            quality_score=float(r.quality_score) if r.quality_score else None,
            tokens_usados=r.tokens_usados,
            costo_usd=float(r.costo_usd) if r.costo_usd else None,
            created_at=r.created_at,
            completed_at=r.completed_at,
        )
        for r in runs
    ]


# (Borradores diff endpoint is implemented in cuentas_cobro.py)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_event(data: dict[str, Any]) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
