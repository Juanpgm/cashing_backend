"""Schemas for Plantilla (templates)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.plantilla import TipoPlantilla


class PlantillaCreate(BaseModel):
    nombre: str = Field(min_length=3, max_length=255)
    tipo: TipoPlantilla
    contenido_html: str = Field(min_length=10)

    model_config = {
        "json_schema_extra": {
            "example": {
                "nombre": "Cuenta de Cobro Estándar",
                "tipo": "cuenta_cobro",
                "contenido_html": "<html><body><h1>Cuenta de Cobro</h1><p>{{entidad}}</p></body></html>",
            }
        }
    }


class PlantillaUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=3, max_length=255)
    tipo: TipoPlantilla | None = None
    contenido_html: str | None = Field(default=None, min_length=10)
    activa: bool | None = None


class PlantillaResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID | None
    nombre: str
    tipo: TipoPlantilla
    activa: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PlantillaRenderRequest(BaseModel):
    """Request to render/fill a template with data."""

    data: dict[str, str | int | float | None]

    model_config = {
        "json_schema_extra": {
            "example": {
                "data": {
                    "entidad": "Ministerio de TIC",
                    "contratista": "Juan Pérez",
                    "valor": "5000000",
                    "periodo": "Abril 2025",
                }
            }
        }
    }


class PlantillaRenderResponse(BaseModel):
    html: str
    pdf_b64: str | None = None
