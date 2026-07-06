"""Pydantic schemas for per-cuenta custom/inferred requirements."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Checklist build mode chosen at the post-creation gate.
RequisitosModo = Literal["estandar", "augment", "reemplazar"]


# ── LLM structured output (response_format) ──────────────────────────────────


class RequisitoInferidoLLM(BaseModel):
    """One requirement inferred by the LLM from a requirements document/text."""

    codigo: str = ""
    etiqueta: str = ""
    descripcion: str = ""
    obligatorio: bool = True
    solo_primera_cuenta: bool = False
    keywords_deteccion: list[str] = Field(default_factory=list)
    mapea_a_estandar: str | None = None


class RequisitosInferidosLLM(BaseModel):
    """Top-level wrapper for the structured list of inferred requirements."""

    requisitos: list[RequisitoInferidoLLM] = Field(default_factory=list)


# ── API request / response ───────────────────────────────────────────────────


class RequisitoCuentaItem(BaseModel):
    """An editable requirement row (preview or persisted custom set).

    ``id`` is present only for already-persisted custom requisitos; it is None
    for freshly inferred items that have not been applied yet.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID | None = None
    codigo: str
    etiqueta: str
    descripcion: str | None = None
    obligatorio: bool = True
    solo_primera_cuenta: bool = False
    tipo_documento_fuente: str | None = None
    keywords_deteccion: list[str] = Field(default_factory=list)
    orden: int = 500
    mapea_a_estandar: str | None = None
    origen: str = "inferido"


class InferirTextoBody(BaseModel):
    """Request body to infer requirements from pasted text."""

    texto: str = Field(min_length=1, max_length=200_000)


class RequisitosInferidosPreview(BaseModel):
    """Non-persisted preview returned by the inference endpoints."""

    requisitos: list[RequisitoCuentaItem] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)


class DefinirRequisitosBody(BaseModel):
    """Apply action: replace the cuenta's custom set and set the build mode."""

    modo: RequisitosModo
    requisitos: list[RequisitoCuentaItem] = Field(default_factory=list)


class RequisitosCuentaSet(BaseModel):
    """Current custom set + build mode for a cuenta (for re-editing)."""

    modo: RequisitosModo | None = None
    requisitos: list[RequisitoCuentaItem] = Field(default_factory=list)
