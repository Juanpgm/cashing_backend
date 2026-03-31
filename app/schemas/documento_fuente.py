"""Schemas for DocumentoFuente and ContratoConfiguracion."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.documento_fuente import TipoDocumentoFuente


class DocumentoFuenteResponse(BaseModel):
    id: uuid.UUID
    nombre: str
    tipo: TipoDocumentoFuente
    contrato_id: uuid.UUID | None
    tiene_texto: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ContratoConfiguracionResponse(BaseModel):
    contrato_id: uuid.UUID
    listo: bool
    tiene_texto_contrato: bool
    tiene_instrucciones: bool
    tiene_plantilla: bool
    tiene_obligaciones: bool
    faltantes: list[str]
    documentos: list[DocumentoFuenteResponse]
    system_prompt: str | None
