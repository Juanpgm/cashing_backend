"""Cache models for SECOP public contracting data (datos.gov.co)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, ForeignKey, Index, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class SecopContrato(UUIDMixin, TimestampMixin, Base):
    """Cache de contratos SECOP — dataset jbjy-vk9h."""

    __tablename__ = "secop_contratos"
    __table_args__ = (
        Index("ix_secop_contratos_cedula", "cedula_contratista"),
        Index("ix_secop_contratos_proceso", "proceso_de_compra"),
        Index("ix_secop_contratos_numero", "numero_contrato"),
    )

    id_contrato_secop: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    cedula_contratista: Mapped[str] = mapped_column(String(30), nullable=False)

    # Relationships
    documentos: Mapped[list[SecopDocumento]] = relationship(back_populates="contrato", lazy="select")
    tipodocproveedor: Mapped[str | None] = mapped_column(String(50))
    nombre_contratista: Mapped[str | None] = mapped_column(String(500))
    nombre_entidad: Mapped[str | None] = mapped_column(String(500))
    nit_entidad: Mapped[str | None] = mapped_column(String(50))
    sector: Mapped[str | None] = mapped_column(String(200))
    departamento: Mapped[str | None] = mapped_column(String(100))
    ciudad: Mapped[str | None] = mapped_column(String(100))
    proceso_de_compra: Mapped[str | None] = mapped_column(String(200))
    numero_contrato: Mapped[str | None] = mapped_column(String(200))
    referencia_del_contrato: Mapped[str | None] = mapped_column(String(500))
    tipo_de_contrato: Mapped[str | None] = mapped_column(String(200))
    modalidad_de_contratacion: Mapped[str | None] = mapped_column(String(200))
    descripcion_del_proceso: Mapped[str | None] = mapped_column(Text)
    estado_contrato: Mapped[str | None] = mapped_column(String(100))
    fecha_de_firma: Mapped[date | None] = mapped_column(Date)
    fecha_inicio: Mapped[date | None] = mapped_column(Date)
    fecha_fin: Mapped[date | None] = mapped_column(Date)
    valor_del_contrato: Mapped[float | None] = mapped_column(Numeric(20, 2))
    valor_pagado: Mapped[float | None] = mapped_column(Numeric(20, 2))
    datos_raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SecopProceso(UUIDMixin, TimestampMixin, Base):
    """Cache de procesos SECOP — dataset p6dx-8zbt."""

    __tablename__ = "secop_procesos"
    __table_args__ = (
        Index("ix_secop_procesos_id", "id_proceso_secop"),
        Index("ix_secop_procesos_nit", "nit_entidad"),
    )

    id_proceso_secop: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    referencia_del_proceso: Mapped[str | None] = mapped_column(String(500))
    nombre_del_procedimiento: Mapped[str | None] = mapped_column(String(500))
    descripcion: Mapped[str | None] = mapped_column(Text)
    entidad: Mapped[str | None] = mapped_column(String(500))
    nit_entidad: Mapped[str | None] = mapped_column(String(50))
    departamento_entidad: Mapped[str | None] = mapped_column(String(100))
    ciudad_entidad: Mapped[str | None] = mapped_column(String(100))
    fase: Mapped[str | None] = mapped_column(String(100))
    modalidad_de_contratacion: Mapped[str | None] = mapped_column(String(200))
    precio_base: Mapped[float | None] = mapped_column(Numeric(20, 2))
    estado_del_procedimiento: Mapped[str | None] = mapped_column(String(100))
    fecha_de_publicacion: Mapped[date | None] = mapped_column(Date)
    adjudicado: Mapped[str | None] = mapped_column(String(10))
    duracion: Mapped[str | None] = mapped_column(String(50))
    unidad_de_duracion: Mapped[str | None] = mapped_column(String(50))
    datos_raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Relationships
    documentos: Mapped[list[SecopDocumento]] = relationship(back_populates="proceso_rel", lazy="select")


class SecopDocumento(UUIDMixin, TimestampMixin, Base):
    """Cache de documentos de contratos SECOP — dataset dmgg-8hin."""

    __tablename__ = "secop_documentos"
    __table_args__ = (
        Index("ix_secop_docs_numero", "numero_contrato"),
        Index("ix_secop_docs_proceso", "proceso"),
    )

    id_documento_secop: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    numero_contrato: Mapped[str | None] = mapped_column(String(200))  # = referencia_del_contrato in contratos
    proceso: Mapped[str | None] = mapped_column(String(200))          # = proceso_de_compra in contratos
    secop_contrato_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("secop_contratos.id"), nullable=True, index=True
    )
    secop_proceso_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("secop_procesos.id"), nullable=True, index=True
    )

    # Relationships
    contrato: Mapped[SecopContrato | None] = relationship(back_populates="documentos")
    proceso_rel: Mapped[SecopProceso | None] = relationship(back_populates="documentos")
    nombre_archivo: Mapped[str | None] = mapped_column(String(500))
    tamanno_archivo: Mapped[str | None] = mapped_column(String(50))
    extension: Mapped[str | None] = mapped_column(String(20))
    descripcion: Mapped[str | None] = mapped_column(String(500))
    fecha_carga: Mapped[date | None] = mapped_column(Date)
    entidad: Mapped[str | None] = mapped_column(String(500))
    nit_entidad: Mapped[str | None] = mapped_column(String(50))
    url_descarga: Mapped[str | None] = mapped_column(String(1000))
    datos_raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
