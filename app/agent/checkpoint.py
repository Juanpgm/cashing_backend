"""SQLAlchemy-native checkpoint store — replaces AsyncPostgresSaver."""
from __future__ import annotations

import uuid
from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import AgentState
from app.core.exceptions import NotFoundError
from app.models.agent_checkpoint import AgentCheckpoint

logger = structlog.get_logger("agent.checkpoint")

_UUID_FIELDS = frozenset({
    "session_id", "user_id", "document_id", "agent_run_id",
    "entity_profile_id", "template_id",
})
_UUID_LIST_FIELDS = frozenset({"uploaded_file_ids"})


def sanitize_state(state: AgentState) -> dict[str, Any]:
    """Remove non-serializable fields before persisting to JSON."""
    return {
        k: (None if k == "document_bytes" else v)
        for k, v in state.items()
        if not k.startswith("_")
    }


def hydrate_state(raw: dict[str, Any]) -> AgentState:
    """Reconstruct typed values from JSON deserialization."""
    out: dict[str, Any] = dict(raw)
    for field_name in _UUID_FIELDS:
        val = out.get(field_name)
        out[field_name] = uuid.UUID(str(val)) if val is not None else None
    for field_name in _UUID_LIST_FIELDS:
        vals = out.get(field_name)
        out[field_name] = [uuid.UUID(str(v)) for v in vals] if vals else []
    if out.get("mode") is not None:
        from app.schemas.agent import AgentMode
        out["mode"] = AgentMode(out["mode"])
    if out.get("messages") is not None:
        from app.schemas.agent import LLMMessage
        out["messages"] = [LLMMessage(**m) for m in out["messages"]]
    return cast(AgentState, out)


async def save_checkpoint(
    db: AsyncSession,
    session_id: uuid.UUID,
    state: AgentState,
    paused_node: str | None,
    *,
    estado: str = "completado",
) -> None:
    """Upsert one checkpoint row per session."""
    result = await db.execute(
        select(AgentCheckpoint).where(AgentCheckpoint.session_id == session_id)
    )
    ckpt = result.scalar_one_or_none()
    clean = sanitize_state(state)

    if ckpt is None:
        ckpt = AgentCheckpoint(
            session_id=session_id,
            paused_node=paused_node,
            estado=estado,
            state_json=clean,
        )
        db.add(ckpt)
    else:
        ckpt.paused_node = paused_node
        ckpt.estado = estado
        ckpt.state_json = clean

    await db.flush()
    logger.info("checkpoint_saved", session_id=str(session_id), estado=estado)


async def load_checkpoint(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> tuple[AgentState, str | None]:
    """Load checkpoint or raise NotFoundError."""
    result = await db.execute(
        select(AgentCheckpoint).where(AgentCheckpoint.session_id == session_id)
    )
    ckpt = result.scalar_one_or_none()
    if ckpt is None:
        raise NotFoundError("AgentCheckpoint", str(session_id))
    return hydrate_state(ckpt.state_json), ckpt.paused_node
