"""Pydantic schemas for the cuenta de cobro document checklist."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.categoria_documento import CategoriaDocumento
from app.models.documento_cuenta_cobro import EstadoRequisito


class RequisitoCatalogoOut(BaseModel):
    """Catalog entry for one required document type.

    ``origen`` is ``"estandar"`` for global-catalog requisitos and ``"cuenta"``
    for custom per-cuenta requisitos. For custom items ``requisito_cuenta_id``
    carries the UUID used to address the row in PATCH calls.
    """

    model_config = ConfigDict(from_attributes=True)

    codigo: str
    etiqueta: str
    descripcion: str | None = None
    obligatorio: bool
    solo_primera_cuenta: bool
    permite_autogen: bool
    tipo_documento_fuente: str | None = None
    orden: int
    origen: str = "estandar"
    requisito_cuenta_id: uuid.UUID | None = None


class SecopCandidatoOut(BaseModel):
    """A SECOP document scored as candidate for a requisito."""

    secop_documento_id: uuid.UUID
    nombre_archivo: str | None = None
    descripcion: str | None = None
    score: Decimal
    url_descarga: str | None = None
    categoria: CategoriaDocumento | None = None
    categoria_confianza: Decimal | None = None
    categoria_override: bool = False


class DocumentoFuenteRef(BaseModel):
    """Minimal reference to a uploaded DocumentoFuente."""

    id: uuid.UUID
    nombre: str
    tipo: str
    categoria: CategoriaDocumento | None = None
    categoria_confianza: Decimal | None = None
    categoria_override: bool = False


class SecopDocumentoRef(BaseModel):
    """Minimal reference to a SECOP cached document."""

    id: uuid.UUID
    nombre_archivo: str | None = None
    descripcion: str | None = None
    url_descarga: str | None = None
    categoria: CategoriaDocumento | None = None
    categoria_confianza: Decimal | None = None
    categoria_override: bool = False


class CategoriaUpdateBody(BaseModel):
    """Request body for manual category override of a document."""

    categoria: CategoriaDocumento


class RequisitoChecklistItem(BaseModel):
    """Per-cuenta state of a single requisito + its candidates."""

    requisito: RequisitoCatalogoOut
    estado: EstadoRequisito
    documento_fuente: DocumentoFuenteRef | None = None
    secop_documento: SecopDocumentoRef | None = None
    documentos_fuente: list[DocumentoFuenteRef] = Field(default_factory=list)
    secop_documentos: list[SecopDocumentoRef] = Field(default_factory=list)
    confianza_deteccion: Decimal | None = None
    observaciones: str | None = None
    candidatos_secop: list[SecopCandidatoOut] = Field(default_factory=list)
    candidatos_documentos_fuente: list[DocumentoFuenteRef] = Field(default_factory=list)
    updated_at: datetime | None = None


class ChecklistResumen(BaseModel):
    total: int
    cumplidos: int
    pendientes: int
    lista_pendientes: list[str] = Field(default_factory=list)
    radicacion_lista: bool


class ArbolEvidenciaItem(BaseModel):
    id: uuid.UUID
    nombre_archivo: str
    tipo_archivo: str
    tamano_bytes: int


class ArbolActividadItem(BaseModel):
    id: uuid.UUID
    descripcion: str
    evidencias: list[ArbolEvidenciaItem] = Field(default_factory=list)


class ArbolObligacionItem(BaseModel):
    obligacion_id: uuid.UUID
    letra: str
    descripcion: str
    tipo: str | None = None
    actividades: list[ArbolActividadItem] = Field(default_factory=list)


class ChecklistResponse(BaseModel):
    """Full checklist payload for one cuenta de cobro.

    When ``requisitos_definidos`` is False the user has not yet resolved the
    post-creation gate (choose standard / infer from document / infer from text),
    so ``items`` is empty and the frontend must show the definition step.
    """

    cuenta_cobro_id: uuid.UUID
    requisitos_definidos: bool = True
    items: list[RequisitoChecklistItem]
    resumen: ChecklistResumen
    arbol_evidencias: list[ArbolObligacionItem] = Field(default_factory=list)


class PatchRequisitoBody(BaseModel):
    """Partial update for a single requisito row.

    Exactly one action field should be provided per call.
    """

    documento_fuente_id: uuid.UUID | None = Field(
        None, description="Link an uploaded DocumentoFuente. Sets estado=CARGADO."
    )
    secop_documento_id: uuid.UUID | None = Field(
        None, description="Link a SECOP cached document. Sets estado=DETECTADO."
    )
    desvincular: bool | None = Field(None, description="If true, removes ALL links and sets estado=PENDIENTE.")
    desvincular_documento_fuente_id: uuid.UUID | None = Field(
        None,
        description=(
            "Removes ONE specific linked DocumentoFuente. If it was the primary link, "
            "the oldest remaining link of the same kind is promoted; if none remain, "
            "estado=PENDIENTE."
        ),
    )
    desvincular_secop_documento_id: uuid.UUID | None = Field(
        None,
        description=(
            "Removes ONE specific linked SecopDocumento. If it was the primary link, "
            "the oldest remaining link of the same kind is promoted; if none remain, "
            "estado=PENDIENTE."
        ),
    )
    no_aplica: bool | None = Field(None, description="If true, marks the requisito as NO_APLICA.")
    cumplido_manual: bool | None = Field(None, description="If true, marks the requisito as CUMPLIDO_MANUAL.")
    observaciones: str | None = Field(None, description="Optional notes (independent of estado).")
