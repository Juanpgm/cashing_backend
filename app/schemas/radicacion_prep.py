"""Pydantic schema for `radicacion_prep_service.preparar_radicacion`'s result
(billing-resilience-templates, slice #7)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.coherence import FindingOut


class PreparaRadicacionResponse(BaseModel):
    """Result of the checklist -> coherence -> packager orchestration."""

    cuenta_cobro_id: uuid.UUID
    storage_key: str
    filename: str
    size_bytes: int
    listo_para_radicar: bool
    pendientes: int
    advertencias_coherencia: list[FindingOut] = Field(default_factory=list)
    es_borrador: bool
