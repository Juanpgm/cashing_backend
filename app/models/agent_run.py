"""AgentRun model — tracks every LangGraph execution."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDMixin


class AgentRun(UUIDMixin, Base):
    __tablename__ = "agent_runs"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversacion_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("conversaciones.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="LangGraph thread_id — maps to conversaciones.id",
    )
    modo: Mapped[str] = mapped_column(String(50), nullable=False)
    nodo_actual: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Last node executed when run completed/failed"
    )
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="en_progreso",
        comment="en_progreso | completado | fallido | pausado_hil",
    )
    tokens_usados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    costo_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    duracion_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_score: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="0.0 – 1.0 from judge node"
    )
    modelo_usado: Mapped[str | None] = mapped_column(String(100), nullable=True)
    creditos_consumidos: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_mensaje: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    usuario: Mapped["Usuario"] = relationship(back_populates="agent_runs")  # type: ignore[name-defined]  # noqa: F821
