"""Pago service — initiate payments and process Wompi webhooks."""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.payments import wompi_adapter
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.credito import TipoCredito
from app.models.pago import EstadoPago, Pago, TipoPago
from app.schemas.pago import IniciarPagoRequest, IniciarPagoResponse, PagoResponse, WompiWebhookEvent
from app.services import notification_service
from app.services.credito_service import agregar_creditos

logger = structlog.get_logger("service.pago")

# Price in COP per credit unit — adjust as needed
_COP_PER_CREDIT = Decimal("1000")  # 1 000 COP / crédito


def _calcular_monto(cantidad_creditos: int) -> Decimal:
    return _COP_PER_CREDIT * cantidad_creditos


async def iniciar_pago(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    req: IniciarPagoRequest,
    usuario_email: str = "",
) -> IniciarPagoResponse:
    """Create a Pago record and get a Wompi checkout URL."""
    pago = Pago(
        usuario_id=usuario_id,
        monto=float(req.monto),
        estado=EstadoPago.PENDIENTE,
        tipo=req.tipo,
    )
    db.add(pago)
    await db.flush()  # get pago.id before calling Wompi

    try:
        result = await wompi_adapter.crear_transaccion(
            usuario_id=usuario_id,
            pago_id=pago.id,
            monto_cop=req.monto,
            redirect_url=req.redirect_url,
        )
    except Exception as exc:
        await db.rollback()
        raise ExternalServiceError("Wompi", str(exc)) from exc

    referencia = result["referencia"]
    pago.referencia_wompi = referencia
    checkout_url: str | None = result.get("data", {}).get("data", {}).get("permalink")

    await db.commit()
    await db.refresh(pago)
    logger.info("pago_iniciado", pago_id=str(pago.id), referencia=referencia)
    return IniciarPagoResponse(
        pago_id=pago.id,
        referencia=referencia,
        checkout_url=checkout_url,
        estado=pago.estado,
    )


async def procesar_webhook_wompi(
    db: AsyncSession,
    evento: WompiWebhookEvent,
) -> None:
    """Handle a verified Wompi webhook event and update Pago + Credito."""
    if evento.event != "transaction.updated":
        logger.debug("webhook_ignorado", wompi_event=evento.event)
        return

    transaction = evento.data.get("transaction", {})
    referencia: str = transaction.get("reference", "")
    status_str: str = transaction.get("status", "")
    amount_cents: int = transaction.get("amount_in_cents", 0)

    if not referencia:
        logger.warning("webhook_sin_referencia")
        return

    result = await db.execute(
        select(Pago).where(Pago.referencia_wompi == referencia)
    )
    pago = result.scalar_one_or_none()
    if pago is None:
        logger.warning("pago_no_encontrado", referencia=referencia)
        return

    if status_str == "APPROVED":
        pago.estado = EstadoPago.APROBADO
        await db.flush()
        # Acreditar créditos proporcional al monto
        monto_cop = Decimal(amount_cents) / 100
        cantidad_creditos = max(1, int(monto_cop / _COP_PER_CREDIT))
        await agregar_creditos(
            db=db,
            usuario_id=pago.usuario_id,
            cantidad=cantidad_creditos,
            tipo=TipoCredito.COMPRA,
            referencia=referencia,
        )
        logger.info(
            "pago_aprobado",
            pago_id=str(pago.id),
            creditos=cantidad_creditos,
        )
        await notification_service.notificar(
            event="pago.aprobado",
            usuario_id=pago.usuario_id,
            titulo="Pago aprobado",
            cuerpo=f"Tu pago fue aprobado. Se acreditaron {cantidad_creditos} créditos.",
            data={"pago_id": str(pago.id), "creditos": cantidad_creditos},
        )
    elif status_str in ("DECLINED", "ERROR", "VOIDED"):
        pago.estado = EstadoPago.RECHAZADO if status_str == "DECLINED" else EstadoPago.ERROR

    await db.commit()


async def listar_pagos(
    db: AsyncSession,
    usuario_id: uuid.UUID,
) -> list[PagoResponse]:
    result = await db.execute(
        select(Pago)
        .where(Pago.usuario_id == usuario_id)
        .order_by(Pago.created_at.desc())
    )
    return [PagoResponse.model_validate(p) for p in result.scalars().all()]
