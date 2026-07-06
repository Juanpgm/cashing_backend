"""AgentCheckpoint model — stores HIL pause state for agent sessions."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class AgentCheckpoint(UUIDMixin, TimestampMixin, Base):
    """Stores serialized AgentState when a session is paused at a HIL node.

    One row per session (unique constraint on session_id — upsert semantics).
    JSON type used (not JSONB) for aiosqlite test compatibility; migration uses JSONB.
    """

    __tablename__ = "agent_checkpoints"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversaciones.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    paused_node: Mapped[str | None] = mapped_column(String(100), nullable=True)
    estado: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="completado"
    )
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    conversacion: Mapped["Conversacion"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="checkpoint",
    )
