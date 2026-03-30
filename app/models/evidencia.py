"""Evidencia model."""

import uuid

from sqlalchemy import BigInteger, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class Evidencia(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "evidencias"

    actividad_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("actividades.id"), nullable=False, index=True
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    nombre_archivo: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo_archivo: Mapped[str] = mapped_column(String(100), nullable=False)
    tamano_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Relationships
    actividad: Mapped["Actividad"] = relationship(back_populates="evidencias")  # type: ignore[name-defined]  # noqa: F821
