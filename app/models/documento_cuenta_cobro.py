"""DocumentoCuentaCobro — link table between a CuentaCobro and a RequisitoDocumento.

Each row represents the state of one required document for a specific cuenta de cobro.
The row may point to either a DocumentoFuente (uploaded by user) or a SecopDocumento
(detected via SECOP cache), or remain pendiente.

A separate candidate table (DocumentoChecklistCandidato) stores SECOP detection
candidates with scoring for the UI to display alternatives.
"""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class EstadoRequisito(enum.StrEnum):
    PENDIENTE = "pendiente"
    DETECTADO = "detectado"  # auto-linked from SECOP (score >= threshold)
    CARGADO = "cargado"  # user uploaded a DocumentoFuente
    CUMPLIDO_MANUAL = "cumplido_manual"  # user marked as fulfilled without a file
    NO_APLICA = "no_aplica"


class DocumentoCuentaCobro(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "documentos_cuenta_cobro"
    __table_args__ = (
        UniqueConstraint("cuenta_cobro_id", "requisito_codigo", name="uq_docccobro_cuenta_requisito"),
        UniqueConstraint("cuenta_cobro_id", "requisito_cuenta_id", name="uq_docccobro_cuenta_reqcuenta"),
        # Each row points to exactly one definition: a catalog code OR a custom requisito.
        CheckConstraint(
            "(requisito_codigo IS NULL) <> (requisito_cuenta_id IS NULL)",
            name="ck_docccobro_una_definicion",
        ),
    )

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cuentas_cobro.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Standard catalog reference (NULL when this row is a custom requisito_cuenta).
    requisito_codigo: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("requisitos_documento.codigo"), nullable=True, index=True
    )
    # Custom per-cuenta requisito reference (NULL when this row is a standard catalog item).
    requisito_cuenta_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("requisitos_cuenta.id", ondelete="CASCADE"), nullable=True, index=True
    )
    estado: Mapped[EstadoRequisito] = mapped_column(
        Enum(EstadoRequisito, name="estado_requisito_documento"),
        nullable=False,
        default=EstadoRequisito.PENDIENTE,
    )
    documento_fuente_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documentos_fuente.id", ondelete="SET NULL"), nullable=True
    )
    secop_documento_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("secop_documentos.id", ondelete="SET NULL"), nullable=True
    )
    confianza_deteccion: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3),
        nullable=True,
        comment="0.000-1.000 SECOP keyword match score when estado=detectado.",
    )
    observaciones: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships (no back_populates needed — one-way)
    documento_fuente: Mapped[DocumentoFuente | None] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[documento_fuente_id], lazy="raise"
    )
    secop_documento: Mapped[SecopDocumento | None] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[secop_documento_id], lazy="raise"
    )
    # All 1:N links for this requisito (superset of the primary documento_fuente/secop_documento
    # above — the primary fields are kept in sync as "the first/preferred link" for backward
    # compatibility). Load explicitly with selectinload where needed.
    vinculos: Mapped[list[DocumentoRequisitoVinculo]] = relationship(  # type: ignore[name-defined]
        "DocumentoRequisitoVinculo",
        foreign_keys="DocumentoRequisitoVinculo.documento_cuenta_cobro_id",
        primaryjoin="DocumentoCuentaCobro.id == DocumentoRequisitoVinculo.documento_cuenta_cobro_id",
        order_by="DocumentoRequisitoVinculo.created_at",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class DocumentoRequisitoVinculo(UUIDMixin, TimestampMixin, Base):
    """One of the (possibly many) documents linked to a single checklist requisito.

    A requisito used to hold AT MOST ONE document via the singular FKs on
    ``DocumentoCuentaCobro`` (``documento_fuente_id`` / ``secop_documento_id``), which
    silently overwrote on every new link (data loss when a requisito legitimately needs
    several files, e.g. RPC original + RPC de adición). This table holds every link;
    the singular FKs on the parent row are kept as "the primary link" for backward
    compatibility with existing readers/tests.
    """

    __tablename__ = "documento_requisito_vinculos"
    __table_args__ = (
        UniqueConstraint("documento_cuenta_cobro_id", "documento_fuente_id", name="uq_docreqvinc_docccobro_fuente"),
        UniqueConstraint("documento_cuenta_cobro_id", "secop_documento_id", name="uq_docreqvinc_docccobro_secop"),
        CheckConstraint(
            "(documento_fuente_id IS NULL) <> (secop_documento_id IS NULL)",
            name="ck_docreqvinc_una_fuente",
        ),
    )

    documento_cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documentos_cuenta_cobro.id", ondelete="CASCADE"), nullable=False, index=True
    )
    documento_fuente_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documentos_fuente.id", ondelete="CASCADE"), nullable=True
    )
    secop_documento_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("secop_documentos.id", ondelete="CASCADE"), nullable=True
    )

    documento_fuente: Mapped[DocumentoFuente | None] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[documento_fuente_id], lazy="raise"
    )
    secop_documento: Mapped[SecopDocumento | None] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[secop_documento_id], lazy="raise"
    )


class DocumentoChecklistCandidato(UUIDMixin, TimestampMixin, Base):
    """SECOP detection candidates for a requisito — top-N shown in UI."""

    __tablename__ = "documento_checklist_candidatos"
    __table_args__ = (
        UniqueConstraint(
            "cuenta_cobro_id",
            "requisito_codigo",
            "secop_documento_id",
            name="uq_doccand_cuenta_req_secop",
        ),
    )

    cuenta_cobro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cuentas_cobro.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requisito_codigo: Mapped[str] = mapped_column(
        String(50), ForeignKey("requisitos_documento.codigo"), nullable=False, index=True
    )
    secop_documento_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("secop_documentos.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)

    # Relationship to fetch document details
    secop_documento: Mapped[SecopDocumento] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[secop_documento_id], lazy="raise"
    )
