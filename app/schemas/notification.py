"""Schemas for outbound notifications."""

import uuid

from pydantic import BaseModel, Field


class NotificationMessage(BaseModel):
    """A user-facing outbound notification, channel-agnostic."""

    event: str  # dotted event key, e.g. "pago.aprobado"
    usuario_id: uuid.UUID
    titulo: str
    cuerpo: str
    data: dict = Field(default_factory=dict)
