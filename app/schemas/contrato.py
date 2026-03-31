"""Schemas for Contrato and Obligacion."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.obligacion import TipoObligacion


class ObligacionCreate(BaseModel):
    descripcion: str = Field(min_length=5, max_length=1000)
    tipo: TipoObligacion
    orden: int = Field(default=0, ge=0)


class ObligacionResponse(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    descripcion: str
    tipo: TipoObligacion
    orden: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ContratoCreate(BaseModel):
    numero_contrato: str = Field(min_length=1, max_length=100)
    objeto: str = Field(min_length=10, max_length=2000)
    valor_total: Decimal = Field(gt=0, decimal_places=2)
    valor_mensual: Decimal = Field(gt=0, decimal_places=2)
    fecha_inicio: date
    fecha_fin: date
    supervisor_nombre: str | None = Field(default=None, max_length=255)
    entidad: str | None = Field(default=None, max_length=255)
    dependencia: str | None = Field(default=None, max_length=255)
    documento_proveedor: str | None = Field(default=None, max_length=30)
    obligaciones: list[ObligacionCreate] = Field(default_factory=list)


class ContratoUpdate(BaseModel):
    numero_contrato: str | None = Field(default=None, min_length=1, max_length=100)
    objeto: str | None = Field(default=None, min_length=10, max_length=2000)
    valor_total: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    valor_mensual: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    fecha_inicio: date | None = None
    fecha_fin: date | None = None
    supervisor_nombre: str | None = Field(default=None, max_length=255)
    entidad: str | None = Field(default=None, max_length=255)
    dependencia: str | None = Field(default=None, max_length=255)
    documento_proveedor: str | None = Field(default=None, max_length=30)


class ContratoResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID
    numero_contrato: str
    objeto: str
    valor_total: Decimal
    valor_mensual: Decimal
    fecha_inicio: date
    fecha_fin: date
    supervisor_nombre: str | None
    entidad: str | None
    dependencia: str | None
    documento_proveedor: str | None
    obligaciones: list[ObligacionResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContratoListItem(BaseModel):
    id: uuid.UUID
    numero_contrato: str
    objeto: str
    valor_total: Decimal
    valor_mensual: Decimal
    fecha_inicio: date
    fecha_fin: date
    entidad: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
