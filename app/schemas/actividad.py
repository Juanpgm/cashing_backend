"""Schemas for Actividad and Evidencia."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

# ── Evidencia (nested inside actividad responses) ────────────────────────────


class EvidenciaResponse(BaseModel):
    id: uuid.UUID
    actividad_id: uuid.UUID
    # Nullable: link-evidence (Gmail/Drive/Calendar discovery) has no file, so
    # evidence_persist_service creates it with these fields NULL on purpose —
    # mirrors app.models.evidencia.Evidencia (all three columns nullable=True).
    storage_key: str | None = None
    nombre_archivo: str
    tipo_archivo: str | None = None
    tamano_bytes: int | None = None
    # External-link fields — set for link evidence (Gmail/Drive/Calendar), left
    # NULL for uploaded files. Mirrors app.schemas.evidencia.EvidenciaResponse,
    # which already reads these from the same Evidencia model correctly.
    fuente: str | None = None
    url: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Actividad ────────────────────────────────────────────────────────────────


class ActividadCreate(BaseModel):
    descripcion: str = Field(min_length=10, max_length=2000)
    justificacion: str | None = Field(default=None, max_length=5000)
    fecha_realizacion: date | None = None
    obligacion_id: uuid.UUID | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "descripcion": "Elaboración del informe mensual de avance de las actividades contractuales",
                "justificacion": "Se elaboró y entregó el informe correspondiente al mes de abril de 2025.",
                "fecha_realizacion": "2025-04-30",
                "obligacion_id": None,
            }
        }
    }


class ActividadUpdate(BaseModel):
    descripcion: str | None = Field(default=None, min_length=10, max_length=2000)
    justificacion: str | None = Field(default=None, max_length=5000)
    fecha_realizacion: date | None = None
    obligacion_id: uuid.UUID | None = None


class ActividadResponse(BaseModel):
    id: uuid.UUID
    cuenta_cobro_id: uuid.UUID
    obligacion_id: uuid.UUID | None
    descripcion: str
    justificacion: str | None
    fecha_realizacion: date | None
    evidencias: list[EvidenciaResponse] = []
    created_at: datetime

    model_config = {"from_attributes": True}
