"""Schemas for CuentaCobro — create, respond, state transitions, PDF."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.cuenta_cobro import EstadoCuentaCobro


class ActividadCreate(BaseModel):
    descripcion: str = Field(min_length=10, max_length=2000)
    justificacion: str | None = Field(default=None, max_length=3000)
    fecha_realizacion: date | None = None
    obligacion_id: uuid.UUID | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "descripcion": "Desarrollo e implementación del módulo de autenticación con OAuth 2.0 para el sistema de información institucional",
                "justificacion": "Se cumple con la obligación técnica de desarrollar los módulos del sistema. Se entregó documentación técnica y código fuente al supervisor.",
                "fecha_realizacion": "2025-03-15",
                "obligacion_id": None,
            }
        }
    }


class ActividadResponse(BaseModel):
    id: uuid.UUID
    descripcion: str
    justificacion: str | None
    fecha_realizacion: date | None
    obligacion_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CuentaCobroCreate(BaseModel):
    contrato_id: uuid.UUID
    mes: int = Field(ge=1, le=12)
    anio: int = Field(ge=2020, le=2099)
    valor: Decimal = Field(gt=0, decimal_places=2)

    model_config = {
        "json_schema_extra": {
            "example": {
                "contrato_id": "00000000-0000-0000-0000-000000000000",
                "mes": 3,
                "anio": 2025,
                "valor": "2000000.00",
            }
        }
    }


class CuentaCobroResponse(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    estado: EstadoCuentaCobro
    valor: Decimal
    pdf_storage_key: str | None
    fecha_envio: datetime | None
    actividades: list[ActividadResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CuentaCobroListItem(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    estado: EstadoCuentaCobro
    valor: Decimal
    pdf_storage_key: str | None
    fecha_envio: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CambiarEstadoRequest(BaseModel):
    estado: EstadoCuentaCobro

    model_config = {
        "json_schema_extra": {
            "example": {"estado": "enviada"}
        }
    }


class GenerarPDFResponse(BaseModel):
    pdf_url: str
    pdf_storage_key: str


class PDFUrlResponse(BaseModel):
    pdf_url: str
