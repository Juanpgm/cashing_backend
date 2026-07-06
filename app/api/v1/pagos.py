"""Pagos API — initiate payments, credit balance, and transaction history."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.pago import (
    BalanceCreditosResponse,
    CreditoResponse,
    IniciarPagoRequest,
    IniciarPagoResponse,
    PagoResponse,
)
from app.services import credito_service, pago_service

logger = structlog.get_logger("api.pagos")

router = APIRouter(prefix="/pagos", tags=["pagos"])
creditos_router = APIRouter(prefix="/creditos", tags=["creditos"])


# ── Pagos ─────────────────────────────────────────────────────────────────────


@router.post(
    "/checkout",
    response_model=IniciarPagoResponse,
    status_code=status.HTTP_201_CREATED,
)
async def iniciar_pago(
    req: IniciarPagoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> IniciarPagoResponse:
    """Inicia un pago con Wompi. Devuelve la URL de checkout."""
    return await pago_service.iniciar_pago(
        db=db,
        usuario_id=user.id,
        req=req,
        usuario_email=user.email,
    )


@router.get("/historial", response_model=list[PagoResponse])
async def historial_pagos(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PagoResponse]:
    """Lista el historial de pagos del usuario autenticado."""
    return await pago_service.listar_pagos(db, user.id)


# ── Créditos ──────────────────────────────────────────────────────────────────


@creditos_router.get("/balance", response_model=BalanceCreditosResponse)
async def balance_creditos(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BalanceCreditosResponse:
    """Devuelve el balance actual de créditos y los últimos movimientos."""
    return await credito_service.obtener_balance(db, user.id)
