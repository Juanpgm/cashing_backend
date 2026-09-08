"""Schemas for Actividad and Evidencia."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

# ── Evidencia (nested inside actividad responses) ────────────────────────────


class EvidenciaResponse(BaseModel):
    """Read schema for an evidencia nested inside an actividad response — either an
    uploaded file or an external link (Gmail/Drive/Calendar discovery).

    Mirrors `app.schemas.evidencia.EvidenciaResponse`: uploaded files populate
    storage_key/tipo_archivo/tamano_bytes; link evidence populates fuente/url
    instead, leaving the file fields NULL (see `app.models.evidencia.Evidencia`).
    """

    id: uuid.UUID
    actividad_id: uuid.UUID
    storage_key: str | None = None
    nombre_archivo: str
    tipo_archivo: str | None = None
    tamano_bytes: int | None = None
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
    justificacion_origen: str
    fecha_realizacion: date | None
    evidencias: list[EvidenciaResponse] = []
    created_at: datetime

    model_config = {"from_attributes": True}
