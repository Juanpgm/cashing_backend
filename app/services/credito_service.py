"""Credito service — balance management and transaction logging."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import InsufficientCreditsError
from app.models.credito import Credito, TipoCredito
from app.models.usuario import Usuario
from app.schemas.pago import BalanceCreditosResponse, CreditoResponse

logger = structlog.get_logger("service.credito")


async def _ledger_balance(db: AsyncSession, usuario_id: uuid.UUID) -> int:
    """SUM of the credit ledger — the single source of truth for the balance."""
    q = await db.execute(
        select(func.coalesce(func.sum(Credito.cantidad), 0)).where(
            Credito.usuario_id == usuario_id
        )
    )
    return int(q.scalar_one())


async def _sync_cache(db: AsyncSession, usuario_id: uuid.UUID) -> int:
    """Set the denormalized ``usuarios.creditos_disponibles`` cache to the ledger sum.

    Must be called within the same transaction as any credit mutation so the cache
    (used by the fast credit gate) never drifts from the ledger. Returns the balance.
    """
    balance = await _ledger_balance(db, usuario_id)
    await db.execute(
        sa_update(Usuario).where(Usuario.id == usuario_id).values(creditos_disponibles=balance)
    )
    return balance


async def obtener_saldo(db: AsyncSession, usuario_id: uuid.UUID) -> int:
    """Current credit balance from the ledger (source of truth) — just the number."""
    return await _ledger_balance(db, usuario_id)


async def obtener_balance(
    db: AsyncSession,
    usuario_id: uuid.UUID,
) -> BalanceCreditosResponse:
    """Return current credit balance and recent transactions."""
    # Aggregate balance via SUM(cantidad) — positive=compra/bonus, negative=consumo
    balance: int = await _ledger_balance(db, usuario_id)

    movimientos_q = await db.execute(
        select(Credito)
        .where(Credito.usuario_id == usuario_id)
        # Secondary key so ties on created_at are deterministic on Postgres
        # (microsecond timestamps can collide; SQLite is second-resolution).
        .order_by(Credito.created_at.desc(), Credito.id.desc())
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
    await db.flush()  # make the new row visible to the SUM below
    await _sync_cache(db, usuario_id)
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
    await db.flush()
    await _sync_cache(db, usuario_id)
    await db.commit()
    logger.info("creditos_consumed", usuario_id=str(usuario_id), cantidad=cantidad, accion=accion)


async def reconciliar_creditos(db: AsyncSession, usuario_id: uuid.UUID) -> int:
    """Fix a drifted cache: set ``creditos_disponibles`` to the ledger sum. Returns it.

    Use to repair rows whose cache diverged before the sync was in place (e.g. a
    Wompi top-up that only wrote the ledger).
    """
    balance = await _sync_cache(db, usuario_id)
    await db.commit()
    return balance


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
