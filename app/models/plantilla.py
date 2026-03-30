"""Plantilla model."""

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TipoPlantilla(enum.StrEnum):
    CUENTA_COBRO = "cuenta_cobro"
    INFORME_ACTIVIDADES = "informe_actividades"
    INFORME_SUPERVISION = "informe_supervision"


class Plantilla(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "plantillas"

    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("usuarios.id"), nullable=True
    )
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    contenido_html: Mapped[str] = mapped_column(Text, nullable=False)
    tipo: Mapped[TipoPlantilla] = mapped_column(
        Enum(TipoPlantilla, name="tipo_plantilla"), nullable=False
    )
    activa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
