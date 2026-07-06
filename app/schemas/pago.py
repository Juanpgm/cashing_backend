"""Schemas for Pago (payments) and Suscripcion."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.pago import EstadoPago, TipoPago
from app.models.suscripcion import PlanSuscripcion


# ── Pago ─────────────────────────────────────────────────────────────────────


class IniciarPagoRequest(BaseModel):
    """Request to initiate a Wompi payment."""

    tipo: TipoPago
    monto: Decimal = Field(gt=0, decimal_places=2)
    plan: PlanSuscripcion | None = None  # required when tipo == SUSCRIPCION
    redirect_url: str | None = None  # URL to redirect after payment

    model_config = {
        "json_schema_extra": {
            "example": {
                "tipo": "creditos",
                "monto": "50000",
                "plan": None,
                "redirect_url": "https://app.cashin.co/pagos/confirmacion",
            }
        }
    }


class PagoResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID
    referencia_wompi: str | None
    monto: float
    estado: EstadoPago
    tipo: TipoPago
    created_at: datetime

    model_config = {"from_attributes": True}


class IniciarPagoResponse(BaseModel):
    pago_id: uuid.UUID
    referencia: str
    checkout_url: str | None = None
    estado: EstadoPago


# ── Credito ───────────────────────────────────────────────────────────────────


class CreditoResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID
    cantidad: int
    tipo: str
    referencia: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class BalanceCreditosResponse(BaseModel):
    balance: int
    movimientos: list[CreditoResponse] = []


# ── Suscripcion ───────────────────────────────────────────────────────────────


class SuscripcionResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID
    plan: PlanSuscripcion
    creditos_mensuales: int
    fecha_inicio: date
    fecha_fin: date | None
    activa: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Wompi Webhook ─────────────────────────────────────────────────────────────


class WompiTransactionData(BaseModel):
    id: str
    reference: str
    status: str
    amount_in_cents: int
    currency: str

    model_config = {"extra": "allow"}


class WompiWebhookEvent(BaseModel):
    event: str
    data: dict  # type: ignore[type-arg]
    sent_at: str | None = None
    timestamp: int | None = None
    signature: dict | None = None  # type: ignore[type-arg]

    model_config = {"extra": "allow"}
