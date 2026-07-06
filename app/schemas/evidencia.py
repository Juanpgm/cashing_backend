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

    model_config = {"from_attributes": True}


class EvidenciaResponse(BaseModel):
    id: uuid.UUID
    actividad_id: uuid.UUID
    storage_key: str
    nombre_archivo: str
    tipo_archivo: str
    tamano_bytes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EvidenciaPresignedResponse(BaseModel):
    """Presigned download URL for a file."""

    id: uuid.UUID
    nombre_archivo: str
    presigned_url: str
    expires_in_seconds: int = Field(default=3600)
