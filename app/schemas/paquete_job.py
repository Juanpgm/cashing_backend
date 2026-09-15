"""Schema for the package-generation background job (radicacion-sin-friccion,
Phase 2 slice 2.7) — returned by both `POST .../paquete/regenerar-async` and
`GET .../paquete/job`."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

from app.schemas.coherence import FindingOut


class PaqueteJobResponse(BaseModel):
    """Mirrors `radicacion_prep_service.RadicacionPrepResultado`'s fields
    (all `None` until `status == "done"`) plus `error`/`error_code` (set only
    when `status == "failed"`)."""

    cuenta_cobro_id: uuid.UUID
    status: str = Field(description="pending | running | done | failed")
    storage_key: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    listo_para_radicar: bool | None = None
    pendientes: int | None = None
    advertencias_coherencia: list[FindingOut] = Field(default_factory=list)
    es_borrador: bool | None = None
    error: str | None = None
    error_code: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("advertencias_coherencia", mode="before")
    @classmethod
    def _none_becomes_empty_list(cls, v: list[object] | None) -> list[object]:
        """`PaqueteJob.advertencias_coherencia` (the JSON column) is `NULL`
        for any job that hasn't reached `done` yet (`pending`/`running`/
        `failed`) — coerce that to `[]` instead of a 500 validation error."""
        return v if v is not None else []
