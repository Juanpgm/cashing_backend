"""Conversacion model — chat history with agent."""

import uuid

from sqlalchemy import JSON, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class Conversacion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "conversaciones"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=False, index=True
    )
    cuenta_cobro_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cuentas_cobro.id"), nullable=True
    )
    mensajes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # type: ignore[type-arg]
