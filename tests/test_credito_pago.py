"""Tests for credito service and pago service (including Wompi webhook)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credito import Credito, TipoCredito
from app.models.pago import EstadoPago, Pago, TipoPago
from app.services import credito_service, pago_service
from app.schemas.pago import IniciarPagoRequest, WompiWebhookEvent

pytestmark = pytest.mark.asyncio


# ── Credito service tests ──────────────────────────────────────────────────────


async def test_balance_inicial_cero(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    resp = await credito_service.obtener_balance(db, user.id)
    # test_user created without any Credito rows
    assert resp.balance == 0


async def test_agregar_creditos(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    await credito_service.agregar_creditos(db, user.id, 50, TipoCredito.BONUS)
    resp = await credito_service.obtener_balance(db, user.id)
    assert resp.balance == 50


async def test_consumir_creditos(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    await credito_service.agregar_creditos(db, user.id, 100, TipoCredito.COMPRA)
    await credito_service.consumir_creditos(db, user.id, 30, "chat_message")
    resp = await credito_service.obtener_balance(db, user.id)
    assert resp.balance == 70


async def test_consumir_mas_que_disponible(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.core.exceptions import InsufficientCreditsError

    user = test_user["user"]
    await credito_service.agregar_creditos(db, user.id, 10, TipoCredito.BONUS)
    with pytest.raises(InsufficientCreditsError):
        await credito_service.consumir_creditos(db, user.id, 100, "overflow")


async def test_historial_creditos(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    await credito_service.agregar_creditos(db, user.id, 50, TipoCredito.COMPRA, "ref001")
    await credito_service.agregar_creditos(db, user.id, 20, TipoCredito.BONUS)
    resp = await credito_service.obtener_balance(db, user.id)
    assert len(resp.movimientos) == 2
    assert resp.balance == 70


async def test_signup_bonus(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.core.config import settings

    user = test_user["user"]
    await credito_service.otorgar_creditos_signup(db, user.id)
    resp = await credito_service.obtener_balance(db, user.id)
    assert resp.balance == settings.FREE_CREDITS_ON_SIGNUP


# ── Pago service tests ─────────────────────────────────────────────────────────


@patch("app.adapters.payments.wompi_adapter.crear_transaccion")
async def test_iniciar_pago(
    mock_wompi: AsyncMock,
    db: AsyncSession,
    test_user: dict[str, Any],
) -> None:
    mock_wompi.return_value = {
        "referencia": "cashin-abc-def",
        "data": {"data": {"permalink": "https://checkout.wompi.co/l/ABC"}},
    }
    user = test_user["user"]
    req = IniciarPagoRequest(tipo=TipoPago.CREDITOS, monto=Decimal("50000"))
    result = await pago_service.iniciar_pago(db, user.id, req)
    assert result.referencia == "cashin-abc-def"
    assert result.checkout_url == "https://checkout.wompi.co/l/ABC"
    assert result.estado == EstadoPago.PENDIENTE


@patch("app.adapters.payments.wompi_adapter.crear_transaccion")
async def test_iniciar_pago_wompi_error(
    mock_wompi: AsyncMock,
    db: AsyncSession,
    test_user: dict[str, Any],
) -> None:
    from app.core.exceptions import ExternalServiceError

    mock_wompi.side_effect = Exception("Connection refused")
    user = test_user["user"]
    req = IniciarPagoRequest(tipo=TipoPago.CREDITOS, monto=Decimal("50000"))
    with pytest.raises(ExternalServiceError):
        await pago_service.iniciar_pago(db, user.id, req)


async def test_procesar_webhook_aprobado_acredita(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    # Crear un pago pendiente manualmente
    pago = Pago(
        usuario_id=user.id,
        referencia_wompi="cashin-test-001",
        monto=50000.0,
        estado=EstadoPago.PENDIENTE,
        tipo=TipoPago.CREDITOS,
    )
    db.add(pago)
    await db.commit()

    evento = WompiWebhookEvent(
        event="transaction.updated",
        data={
            "transaction": {
                "id": "tx_123",
                "reference": "cashin-test-001",
                "status": "APPROVED",
                "amount_in_cents": 5_000_000,
                "currency": "COP",
            }
        },
    )
    await pago_service.procesar_webhook_wompi(db, evento)
    await db.refresh(pago)
    assert pago.estado == EstadoPago.APROBADO

    balance = await credito_service.obtener_balance(db, user.id)
    assert balance.balance > 0


async def test_procesar_webhook_rechazado(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    pago = Pago(
        usuario_id=user.id,
        referencia_wompi="cashin-test-002",
        monto=50000.0,
        estado=EstadoPago.PENDIENTE,
        tipo=TipoPago.CREDITOS,
    )
    db.add(pago)
    await db.commit()

    evento = WompiWebhookEvent(
        event="transaction.updated",
        data={
            "transaction": {
                "id": "tx_456",
                "reference": "cashin-test-002",
                "status": "DECLINED",
                "amount_in_cents": 5_000_000,
                "currency": "COP",
            }
        },
    )
    await pago_service.procesar_webhook_wompi(db, evento)
    await db.refresh(pago)
    assert pago.estado == EstadoPago.RECHAZADO


async def test_procesar_webhook_evento_ignorado(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """Non-transaction events should be silently ignored."""
    evento = WompiWebhookEvent(event="payment_link.paid", data={})
    await pago_service.procesar_webhook_wompi(db, evento)  # no exception


# ── Webhook API tests ──────────────────────────────────────────────────────────


async def test_webhook_endpoint_returns_ok(client: AsyncClient) -> None:
    payload = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": "tx_999",
                "reference": "cashin-nonexistent",
                "status": "APPROVED",
                "amount_in_cents": 5_000_000,
                "currency": "COP",
            }
        },
    }
    resp = await client.post("/api/v1/webhooks/wompi", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_webhook_invalid_signature_rejected(client: AsyncClient) -> None:
    """A request with an invalid X-Wompi-Signature must be rejected."""
    from app.core.config import settings

    # Only test if there's a real secret configured
    original = settings.WOMPI_EVENTS_SECRET
    settings.WOMPI_EVENTS_SECRET = "real_secret_12345"

    try:
        payload = {"event": "transaction.updated", "data": {}}
        resp = await client.post(
            "/api/v1/webhooks/wompi",
            json=payload,
            headers={"X-Wompi-Signature": "invalidsig", "X-Wompi-Timestamp": "1234567890"},
        )
        assert resp.status_code == 401
    finally:
        settings.WOMPI_EVENTS_SECRET = original


# ── Pagos API tests ────────────────────────────────────────────────────────────


@patch("app.adapters.payments.wompi_adapter.crear_transaccion")
async def test_api_iniciar_pago(
    mock_wompi: AsyncMock,
    client: AsyncClient,
    test_user: dict[str, Any],
) -> None:
    mock_wompi.return_value = {
        "referencia": "cashin-api-001",
        "data": {"data": {"permalink": "https://checkout.wompi.co/l/XYZ"}},
    }
    resp = await client.post(
        "/api/v1/pagos/checkout",
        headers=test_user["headers"],
        json={"tipo": "creditos", "monto": "50000"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["referencia"] == "cashin-api-001"


async def test_api_balance_creditos(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
) -> None:
    user = test_user["user"]
    await credito_service.agregar_creditos(db, user.id, 100, TipoCredito.BONUS)
    resp = await client.get("/api/v1/creditos/balance", headers=test_user["headers"])
    assert resp.status_code == 200
    assert resp.json()["balance"] == 100
