"""DocumentoFuente model — uploaded source documents (contracts, instructions)."""

import enum
import uuid

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TipoDocumentoFuente(enum.StrEnum):
    CONTRATO = "contrato"
    INSTRUCCIONES = "instrucciones"
    PLANTILLA = "plantilla"


class DocumentoFuente(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "documentos_fuente"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=False, index=True
    )
    contrato_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("contratos.id"), nullable=True, index=True
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[TipoDocumentoFuente] = mapped_column(
        Enum(TipoDocumentoFuente, name="tipo_documento_fuente"), nullable=False
    )
    texto_extraido: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
