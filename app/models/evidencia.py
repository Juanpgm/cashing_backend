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
    # Uploaded-file fields — set for stored evidence, left NULL for link evidence.
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    nombre_archivo: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo_archivo: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tamano_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # External-link fields — set for link evidence (e.g. Gmail/Drive/Calendar
    # discovery results), left NULL for uploaded files. A row is EITHER a
    # stored file (storage_key set) OR an external link (url set).
    fuente: Mapped[str | None] = mapped_column(String(50), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Relationships
    actividad: Mapped["Actividad"] = relationship(back_populates="evidencias")  # type: ignore[name-defined]  # noqa: F821
