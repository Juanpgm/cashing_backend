"""DocumentoFuente model — uploaded source documents (contracts, instructions)."""

import enum
import uuid

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Index, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.categoria_documento import CategoriaDocumento


class TipoDocumentoFuente(enum.StrEnum):
    CONTRATO = "contrato"
    INSTRUCCIONES = "instrucciones"
    PLANTILLA = "plantilla"
    # Checklist requirements for cuenta de cobro (added in migration 011)
    RPC = "rpc"
    SEGURIDAD_SOCIAL = "seguridad_social"
    COMPROBANTE_PAGO_SS = "comprobante_pago_ss"
    INFORME_ACTIVIDADES = "informe_actividades"
    INFORME_SUPERVISION = "informe_supervision"
    DS_CONSECUTIVO = "ds_consecutivo"
    CEDULA = "cedula"
    RUT = "rut"
    FICHA_TECNICA = "ficha_tecnica"
    ACTA_INICIO = "acta_inicio"
    DEPENDIENTES = "dependientes"


class DocumentoFuente(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "documentos_fuente"

    usuario_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("usuarios.id"), nullable=False, index=True)
    contrato_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("contratos.id"), nullable=True, index=True)
    # Strict per-cuenta scoping: a document uploaded/generated through a cuenta's checklist
    # belongs to that cuenta and must never surface in another cuenta of the same contract.
    # NULL = contract-level document not tied to any single cuenta.
    cuenta_cobro_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cuentas_cobro.id", ondelete="SET NULL"), nullable=True, index=True
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[TipoDocumentoFuente] = mapped_column(
        Enum(TipoDocumentoFuente, name="tipo_documento_fuente"), nullable=False
    )
    texto_extraido: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]

    # Document classification (global axis — independent from checklist per-cuenta state)
    categoria: Mapped[CategoriaDocumento] = mapped_column(
        Enum(CategoriaDocumento, name="categoria_documento", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=CategoriaDocumento.OTROS,
        server_default=CategoriaDocumento.OTROS.value,
    )
    categoria_confianza: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    categoria_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        Index("ix_documentos_fuente_dedup", "usuario_id", "nombre", "tipo", "contrato_id", "cuenta_cobro_id"),
    )
