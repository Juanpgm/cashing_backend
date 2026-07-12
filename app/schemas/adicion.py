"""Pydantic schemas for contract Adición events (billing-resilience-templates, slice #4)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.adicion_contrato import TipoAdicion


class AdicionCreate(BaseModel):
    tipo: TipoAdicion
    numero: int = Field(ge=1, description="Sequential adición number for the contract.")
    fecha_evento: date
    rpc_nuevo: str | None = None
    cdp_nuevo: str | None = None
    valor_adicion: Decimal | None = None
    nueva_fecha_fin: date | None = None
    descripcion: str = ""


class AdicionOut(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    tipo: TipoAdicion
    numero: int
    rpc_nuevo: str | None
    cdp_nuevo: str | None
    valor_adicion: Decimal | None
    nueva_fecha_fin: date | None
    descripcion: str
    fecha_evento: date
    created_at: datetime

    model_config = {"from_attributes": True}
