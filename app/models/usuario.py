"""Usuario model."""

import enum

from sqlalchemy import Boolean, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDMixin


class RolUsuario(enum.StrEnum):
    CONTRATISTA = "contratista"
    ADMIN = "admin"


class Usuario(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "usuarios"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    cedula: Mapped[str | None] = mapped_column(String(20), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[RolUsuario] = mapped_column(
        Enum(RolUsuario, name="rol_usuario"),
        nullable=False,
        default=RolUsuario.CONTRATISTA,
    )
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    creditos_disponibles: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    contratos: Mapped[list["Contrato"]] = relationship(back_populates="usuario", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
    suscripciones: Mapped[list["Suscripcion"]] = relationship(back_populates="usuario", lazy="selectin")  # type: ignore[name-defined]  # noqa: F821
