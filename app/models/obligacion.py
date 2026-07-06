"""Obligacion model."""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TipoObligacion(enum.StrEnum):
    GENERAL = "general"
    ESPECIFICA = "especifica"


class Obligacion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "obligaciones"

    contrato_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contratos.id"), nullable=False, index=True
    )
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    tipo: Mapped[TipoObligacion] = mapped_column(
        Enum(TipoObligacion, name="tipo_obligacion"), nullable=False
    )
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    etiqueta: Mapped[str] = mapped_column(String(8), nullable=False, default="")

    # Semantic search embedding (text-embedding-004, 1536 dims, stored as Text JSON)
    # Use app.agent.tools.vector_search.encode/decode helpers to convert to/from list[float]
    embedding: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON-encoded float list; cast to vector(1536) in pgvector queries",
    )

    # Relationships
    contrato: Mapped["Contrato"] = relationship(back_populates="obligaciones")  # type: ignore[name-defined]  # noqa: F821
    actividades: Mapped[list["Actividad"]] = relationship(back_populates="obligacion", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
