"""Schemas for Evidencia (file evidence attached to actividades)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EvidenciaUploadResponse(BaseModel):
    """Returned after uploading an evidence file."""

    id: uuid.UUID
    actividad_id: uuid.UUID
    storage_key: str
    nombre_archivo: str
    tipo_archivo: str
    tamano_bytes: int
    presigned_url: str | None = None
    created_at: datetime
    duplicada: bool = Field(
        default=False,
        description="True cuando el archivo ya existía (mismo contenido) y no se volvió a subir.",
    )

    model_config = {"from_attributes": True}


class EvidenciaResponse(BaseModel):
    """Read schema for an evidencia — either an uploaded file or an external link.

    Uploaded files populate storage_key/tipo_archivo/tamano_bytes; link evidence
    (from the evidence-discovery agent) populates fuente/url instead.
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


class EvidenciaClasificadaResponse(BaseModel):
    """Returned after uploading a cuenta-scoped evidence file (POST .../evidencias/subir).

    Unlike `EvidenciaUploadResponse` (per-actividad upload, actividad already exists),
    this reports the auto-classification outcome against the contract's obligaciones —
    the upload endpoint find-or-creates a stub `Actividad` under the hood.
    """

    id: uuid.UUID
    actividad_id: uuid.UUID
    obligacion_id: uuid.UUID | None = None
    obligacion_etiqueta: str | None = None
    nombre_archivo: str
    tipo_archivo: str
    tamano_bytes: int
    presigned_url: str | None = None
    clasificado: bool
    created_at: datetime
    duplicada: bool = Field(
        default=False,
        description="True cuando el archivo ya existía (mismo contenido) y no se volvió a subir.",
    )

    model_config = {"from_attributes": True}


class EvidenciasCuentaSubidaResponse(BaseModel):
    """Response for POST .../evidencias/subir — wraps the per-file classification
    results plus batch-level avisos (skipped/omitted .zip/.rar archive members,
    rar-support degradation, member-count cap truncation). Mirrors
    `AnalizarArchivoResponse`'s resultados+avisos shape used for the analogous
    `archivo:analizar` plantillas-organismo endpoint.
    """

    resultados: list[EvidenciaClasificadaResponse]
    avisos: list[str] = Field(default_factory=list)


class EvidenciaPresignedResponse(BaseModel):
    """Presigned download URL for a file."""

    id: uuid.UUID
    nombre_archivo: str
    presigned_url: str
    expires_in_seconds: int = Field(default=3600)
