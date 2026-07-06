"""Webhooks API — receive and process Wompi payment events."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from app.adapters.payments.wompi_adapter import verificar_firma_webhook
from app.core.database import get_db
from app.schemas.pago import WompiWebhookEvent
from app.services import pago_service

logger = structlog.get_logger("api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/wompi", status_code=status.HTTP_200_OK)
async def wompi_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_wompi_signature: str | None = Header(default=None, alias="X-Wompi-Signature"),
    x_wompi_timestamp: str | None = Header(default=None, alias="X-Wompi-Timestamp"),
) -> dict:  # type: ignore[type-arg]
    """Receive Wompi payment webhook events.

    Verifies HMAC-SHA256 signature before processing.
    Returns 200 immediately; processing happens in background.
    """
    payload_bytes = await request.body()

    if x_wompi_signature:
        timestamp = x_wompi_timestamp or ""
        if not verificar_firma_webhook(
            payload_bytes=payload_bytes,
            timestamp=timestamp,
            checksum=x_wompi_signature,
        ):
            logger.warning("wompi_webhook_invalid_signature")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    evento = WompiWebhookEvent.model_validate_json(payload_bytes)
    logger.info("wompi_webhook_received", wompi_event=evento.event)

    async def _process() -> None:
        async for db in get_db():
            try:
                await pago_service.procesar_webhook_wompi(db=db, evento=evento)
            except Exception:
                logger.exception("wompi_webhook_processing_error")

    background_tasks.add_task(_process)
    return {"ok": True}
