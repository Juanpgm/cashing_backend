"""Credito service — balance management and transaction logging."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import InsufficientCreditsError
from app.models.credito import Credito, TipoCredito
from app.schemas.pago import BalanceCreditosResponse, CreditoResponse

logger = structlog.get_logger("service.credito")


async def obtener_balance(
    db: AsyncSession,
    usuario_id: uuid.UUID,
) -> BalanceCreditosResponse:
    """Return current credit balance and recent transactions."""
    # Aggregate balance via SUM(cantidad) — positive=compra/bonus, negative=consumo
    balance_q = await db.execute(
        select(func.coalesce(func.sum(Credito.cantidad), 0)).where(
            Credito.usuario_id == usuario_id
        )
    )
    balance: int = int(balance_q.scalar_one())

    movimientos_q = await db.execute(
        select(Credito)
        .where(Credito.usuario_id == usuario_id)
        .order_by(Credito.created_at.desc())
        .limit(50)
    )
    movimientos = [CreditoResponse.model_validate(c) for c in movimientos_q.scalars().all()]
    return BalanceCreditosResponse(balance=balance, movimientos=movimientos)


async def agregar_creditos(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cantidad: int,
    tipo: TipoCredito,
    referencia: str | None = None,
) -> CreditoResponse:
    """Add (positive) credits to user balance."""
    credito = Credito(
        usuario_id=usuario_id,
        cantidad=abs(cantidad),
        tipo=tipo,
        referencia=referencia,
    )
    db.add(credito)
    await db.commit()
    await db.refresh(credito)
    logger.info("creditos_added", usuario_id=str(usuario_id), cantidad=cantidad, tipo=tipo)
    return CreditoResponse.model_validate(credito)


async def consumir_creditos(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cantidad: int,
    accion: str,
) -> None:
    """Deduct credits for an action. Raises InsufficientCreditsError if balance too low."""
    balance_resp = await obtener_balance(db, usuario_id)
    if balance_resp.balance < cantidad:
        raise InsufficientCreditsError(required=cantidad, available=balance_resp.balance)

    credito = Credito(
        usuario_id=usuario_id,
        cantidad=-abs(cantidad),
        tipo=TipoCredito.CONSUMO,
        referencia=accion,
    )
    db.add(credito)
    await db.commit()
    logger.info("creditos_consumed", usuario_id=str(usuario_id), cantidad=cantidad, accion=accion)


async def otorgar_creditos_signup(
    db: AsyncSession,
    usuario_id: uuid.UUID,
) -> CreditoResponse:
    """Grant FREE_CREDITS_ON_SIGNUP bonus credits to new user."""
    return await agregar_creditos(
        db=db,
        usuario_id=usuario_id,
        cantidad=settings.FREE_CREDITS_ON_SIGNUP,
        tipo=TipoCredito.BONUS,
        referencia="signup_bonus",
    )
