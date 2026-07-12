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
