"""Pydantic schemas for per-organism template ingestion (billing-resilience-
templates, slice #5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TipoDocumentoPlantilla = Literal["informe_actividades", "informe_supervision"]


# ── LLM structured output (response_format) ──────────────────────────────────


class EstructuraPlantillaLLM(BaseModel):
    """Structure the LLM extracts from an institutional informe template.

    ``anexo_refs`` holds the literal anexo-reference strings found in the
    template (e.g. "Ver Anexo: Carpeta /5. EVIDENCIAS/A1"), preserved verbatim
    per the spec's "Anexo reference preserved verbatim" requirement — a
    superset of design D5's shorthand `anexo_refs: bool` (see
    `requisito_inference_service` module docstring for the rationale).
    """

    columnas: list[str] = Field(default_factory=list)
    secciones: list[str] = Field(default_factory=list)
    anexo_refs: list[str] = Field(default_factory=list)
    notas: str = ""


# ── API request / response ───────────────────────────────────────────────────


class PlantillaOrganismoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entidad: str
    tipo_documento: str
    formato: str
    estructura_json: dict[str, object]
    fuente_documento_id: uuid.UUID | None
    created_at: datetime


class IngerirPlantillaOrganismoResponse(BaseModel):
    """Result of an ingestion attempt. `persistida=False` means graceful
    degradation ran — no structure was persisted, no error was raised."""

    persistida: bool
    plantilla: PlantillaOrganismoOut | None = None
    avisos: list[str] = Field(default_factory=list)


class ObtenerPlantillaOrganismoResponse(BaseModel):
    encontrada: bool
    plantilla: PlantillaOrganismoOut | None = None


# ── Clonable-template campo detection (docx clone, phases 2-3) ───────────────


class CampoPlantillaLLM(BaseModel):
    """One variable field the LLM locates in a FILLED example template.

    Mirrors the campo-dict contract consumed by `docx_clone_service` — see its
    module docstring for the address scheme and modo semantics.
    """

    direccion: str
    etiqueta: str = ""
    campo: str
    valor_ejemplo: str = ""
    modo: Literal["cell", "substring", "checkbox", "justificacion"] = "cell"
    # checkbox only: the OTHER (unmarked) option and its marker text.
    direccion_alternativa: str = ""
    valor_alternativo: str = ""


class CamposPlantillaLLM(BaseModel):
    campos: list[CampoPlantillaLLM] = Field(default_factory=list)


class CampoResponse(BaseModel):
    campo: str
    etiqueta: str
    modo: str


class PlantillaOrganismoResponse(BaseModel):
    """Clone-oriented view of a PlantillaOrganismo. `id=None` + `clonable=False`
    means graceful degradation ran — nothing was persisted, only avisos."""

    id: uuid.UUID | None = None
    tipo_documento: str | None = None
    formato: str | None = None
    clonable: bool = False
    avisos: list[str] = Field(default_factory=list)
    fuente_documento_id: uuid.UUID | None = None
    # Campos grouped by scope prefix: contrato / contratista / cuota / computed / justificacion.
    campos: dict[str, list[CampoResponse]] = Field(default_factory=dict)


class PlantillaOrganismoIngestRequest(BaseModel):
    documento_fuente_id: uuid.UUID


class FormatoValoresResponse(BaseModel):
    """Resolved campo values + computed financials for the clone-fill preview."""

    valores: dict[str, str]
    avisos: list[str] = Field(default_factory=list)


# ── Archive ingestion (formato-replicacion-inteligente, slice 1) ─────────────

TipoDocumentoArchivo = Literal["informe_actividades", "informe_supervision", "cuenta_cobro", "documento_soporte"]


class MiembroClasificadoLLM(BaseModel):
    """One archive member's AI-proposed `tipo_documento`, from the batched
    classification LLM call. `tipo_documento=None` means low confidence /
    unrecognized — the member is surfaced for manual pick, never dropped."""

    filename: str
    tipo_documento: TipoDocumentoArchivo | None = None
    confianza: float = 0.0


class ClasificacionArchivoLLM(BaseModel):
    miembros: list[MiembroClasificadoLLM] = Field(default_factory=list)


class MiembroPropuesto(BaseModel):
    documento_fuente_id: uuid.UUID
    filename: str
    tipo_propuesto: TipoDocumentoArchivo | None = None
    confianza: float = 0.0


class AnalizarArchivoResponse(BaseModel):
    propuestas: list[MiembroPropuesto] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)


class MiembroConfirmado(BaseModel):
    documento_fuente_id: uuid.UUID
    tipo_documento: TipoDocumentoArchivo


class ConfirmarArchivoRequest(BaseModel):
    miembros: list[MiembroConfirmado]


class MiembroIngestaResultado(BaseModel):
    documento_fuente_id: uuid.UUID
    filename: str
    persistida: bool
    avisos: list[str] = Field(default_factory=list)


class ConfirmarArchivoResponse(BaseModel):
    resultados: list[MiembroIngestaResultado] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)
