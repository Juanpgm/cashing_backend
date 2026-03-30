"""Credito model — credit balance tracking."""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TipoCredito(enum.StrEnum):
    COMPRA = "compra"
    CONSUMO = "consumo"
    BONUS = "bonus"


class Credito(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "creditos"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=False, index=True
    )
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo: Mapped[TipoCredito] = mapped_column(
        Enum(TipoCredito, name="tipo_credito"), nullable=False
    )
    referencia: Mapped[str | None] = mapped_column(String(255), nullable=True)
