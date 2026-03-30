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


class GenerarPDFResponse(BaseModel):
    pdf_url: str
    pdf_storage_key: str


class PDFUrlResponse(BaseModel):
    pdf_url: str
