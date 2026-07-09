"""Outbound notification tests — service dispatch, channel selection, adapters, fail-open,
and the payment-approved integration trigger."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.notification.log_adapter import LogNotificationAdapter
from app.adapters.notification.webhook_adapter import WebhookNotificationAdapter
from app.core.config import settings
from app.models.pago import EstadoPago, Pago, TipoPago
from app.schemas.notification import NotificationMessage
from app.schemas.pago import WompiWebhookEvent
from app.services import notification_service, pago_service


# ---------------------------------------------------------------------------
# Service dispatch + fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notificar_noop_when_disabled() -> None:
    """Default (disabled) → returns False and never touches an adapter."""
    assert settings.NOTIFICATIONS_ENABLED is False
    sent = await notification_service.notificar(
        event="test.event", usuario_id=uuid.uuid4(), titulo="t", cuerpo="c"
    )
    assert sent is False


@pytest.mark.asyncio
async def test_notificar_sends_via_log_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFICATION_CHANNEL", "log")
    sent = await notification_service.notificar(
        event="test.event", usuario_id=uuid.uuid4(), titulo="t", cuerpo="c"
    )
    assert sent is True


@pytest.mark.asyncio
async def test_notificar_fails_open_on_adapter_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error is swallowed and reported as False, never raised."""
    monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFICATION_CHANNEL", "log")
    with patch.object(LogNotificationAdapter, "send", new=AsyncMock(side_effect=RuntimeError("boom"))):
        sent = await notification_service.notificar(
            event="test.event", usuario_id=uuid.uuid4(), titulo="t", cuerpo="c"
        )
    assert sent is False


@pytest.mark.asyncio
async def test_channel_selection_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFICATION_CHANNEL", "webhook")
    monkeypatch.setattr(settings, "NOTIFICATION_WEBHOOK_URL", "https://hooks.example.com/x")
    adapter = notification_service._get_adapter()
    assert isinstance(adapter, WebhookNotificationAdapter)


@pytest.mark.asyncio
async def test_channel_falls_back_to_log_when_webhook_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFICATION_CHANNEL", "webhook")
    monkeypatch.setattr(settings, "NOTIFICATION_WEBHOOK_URL", "")
    adapter = notification_service._get_adapter()
    assert isinstance(adapter, LogNotificationAdapter)


# ---------------------------------------------------------------------------
# Webhook adapter transport
# ---------------------------------------------------------------------------


class _FakeResponse:
    def raise_for_status(self) -> None:  # noqa: D401
        return None


class _FakeAsyncClient:
    last_post: tuple[str, dict] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, url: str, json: dict) -> _FakeResponse:
        _FakeAsyncClient.last_post = (url, json)
        return _FakeResponse()


@pytest.mark.asyncio
async def test_webhook_adapter_posts_json() -> None:
    _FakeAsyncClient.last_post = None
    with patch("app.adapters.notification.webhook_adapter.httpx.AsyncClient", _FakeAsyncClient):
        adapter = WebhookNotificationAdapter("https://hooks.example.com/x")
        msg = NotificationMessage(
            event="pago.aprobado", usuario_id=uuid.uuid4(), titulo="t", cuerpo="c", data={"k": 1}
        )
        await adapter.send(msg)

    assert _FakeAsyncClient.last_post is not None
    url, payload = _FakeAsyncClient.last_post
    assert url == "https://hooks.example.com/x"
    assert payload["event"] == "pago.aprobado"
    assert payload["data"] == {"k": 1}


# ---------------------------------------------------------------------------
# Integration: payment approved triggers a notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_approved_triggers_notification(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    pago = Pago(
        usuario_id=user.id,
        referencia_wompi="cashin-notif-001",
        monto=500000.0,
        estado=EstadoPago.PENDIENTE,
        tipo=TipoPago.CREDITOS,
    )
    db.add(pago)
    await db.commit()

    evento = WompiWebhookEvent(
        event="transaction.updated",
        data={
            "transaction": {
                "reference": "cashin-notif-001",
                "status": "APPROVED",
                "amount_in_cents": 50_000_000,
            }
        },
    )

    with patch(
        "app.services.notification_service.notificar", new=AsyncMock(return_value=True)
    ) as mock_notificar:
        await pago_service.procesar_webhook_wompi(db, evento)

    mock_notificar.assert_awaited_once()
    assert mock_notificar.await_args.kwargs["event"] == "pago.aprobado"
    assert mock_notificar.await_args.kwargs["usuario_id"] == user.id
