"""Wompi payment adapter — create transactions and verify webhook signatures."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from decimal import Decimal

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger("adapter.wompi")

_WOMPI_API = settings.WOMPI_API_URL


def _build_reference(usuario_id: uuid.UUID, pago_id: uuid.UUID) -> str:
    """Create a unique Wompi reference: cashin-{uid_short}-{pago_short}."""
    return f"cashin-{str(usuario_id)[:8]}-{str(pago_id)[:8]}"


async def crear_transaccion(
    usuario_id: uuid.UUID,
    pago_id: uuid.UUID,
    monto_cop: Decimal,
    redirect_url: str | None = None,
) -> dict:  # type: ignore[type-arg]
    """Create a Wompi payment link and return the raw API response.

    Wompi amounts are in centavos (COP × 100).
    """
    referencia = _build_reference(usuario_id, pago_id)
    amount_in_cents = int(monto_cop * 100)

    payload = {
        "amount_in_cents": amount_in_cents,
        "currency": "COP",
        "customer_email": "",  # filled by caller if available
        "reference": referencia,
        "redirect_url": redirect_url or settings.WOMPI_API_URL.replace("sandbox.wompi.co/v1", ""),
        "payment_method_types": ["CARD", "PSE", "BANCOLOMBIA_TRANSFER"],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_WOMPI_API}/payment_links",
            headers={"Authorization": f"Bearer {settings.WOMPI_PRIVATE_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    logger.info("wompi_transaction_created", referencia=referencia, amount_cents=amount_in_cents)
    return {"referencia": referencia, "data": data}


async def consultar_transaccion(referencia: str) -> dict:  # type: ignore[type-arg]
    """Fetch a Wompi transaction by reference and return status."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_WOMPI_API}/transactions",
            headers={"Authorization": f"Bearer {settings.WOMPI_PRIVATE_KEY}"},
            params={"reference": referencia},
        )
        resp.raise_for_status()
        return resp.json()


def verificar_firma_webhook(payload_bytes: bytes, timestamp: str, checksum: str) -> bool:
    """Verify Wompi webhook HMAC-SHA256 signature.

    Wompi signature format: SHA256(concatenate(payload_bytes + timestamp + events_secret))
    """
    if not settings.WOMPI_EVENTS_SECRET or settings.WOMPI_EVENTS_SECRET.startswith("test_"):
        # In test/sandbox mode allow unsigned events
        return True

    concat = payload_bytes + timestamp.encode() + settings.WOMPI_EVENTS_SECRET.encode()
    expected = hashlib.sha256(concat).hexdigest()
    return hmac.compare_digest(expected, checksum)
