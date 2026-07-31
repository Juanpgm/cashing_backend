"""Schemas for background evidence classification jobs (evidence-classification-
jobs capability) and evidencia<->obligación reclassification (evidence-
reclassification capability)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ClasificacionJobResponse(BaseModel):
    """Returned by POST .../evidencias/clasificar — the job just (re)triggered."""

    cuenta_cobro_id: uuid.UUID
    status: str = Field(description="pending | running | done | failed")
    total: int
    procesadas: int
    error: str | None = None

    model_config = {"from_attributes": True}


class ObligacionSugeridaOut(BaseModel):
    """One suggested/confirmed link for a given evidencia."""

    obligacion_id: uuid.UUID | None
    confianza: str | None = Field(description="alta | media | baja | null (no_aplica rows)")
    status: str = Field(description="proposed | confirmed | rejected | no_aplica")
    reasoning: str | None = None


class EvidenciaClasificacionOut(BaseModel):
    """Per-file progress row (evidence-classification-jobs: Per-file/per-
    obligación progress status endpoint)."""

    id: uuid.UUID
    nombre_archivo: str
    estado: str = Field(description="pendiente | clasificado | failed_retryable")
    obligaciones: list[ObligacionSugeridaOut]


class ClasificacionEstadoResponse(BaseModel):
    """Returned by GET .../evidencias/clasificacion."""

    cuenta_cobro_id: uuid.UUID
    status: str = Field(description="pending | running | done | failed")
    total: int
    procesadas: int
    error: str | None = None
    evidencias: list[EvidenciaClasificacionOut]


class ReclasificacionRequest(BaseModel):
    """Full user authority over one evidencia's obligación links — any
    combination of add/remove/confirm/no_aplica succeeds independently, no
    approval gate (evidence-reclassification capability)."""

    add: list[uuid.UUID] = Field(default_factory=list)
    remove: list[uuid.UUID] = Field(default_factory=list)
    confirm: list[uuid.UUID] = Field(default_factory=list)
    no_aplica: bool = False


class EnlaceObligacionOut(BaseModel):
    obligacion_id: uuid.UUID | None
    confianza: str | None
    score: float | None
    status: str
    source: str
    reasoning: str | None = None

    model_config = {"from_attributes": True}


class ReclasificacionResponse(BaseModel):
    evidencia_id: uuid.UUID
    obligaciones: list[EnlaceObligacionOut]
