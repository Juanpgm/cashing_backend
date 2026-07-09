"""Notification service — dispatches outbound notifications through the configured channel.

Fail-open by design: notifications are a side effect and must never break the main
business flow (payments, state changes). When disabled or on transport error, the
caller's operation proceeds unaffected.
"""

from __future__ import annotations

import uuid

import structlog

from app.adapters.notification.log_adapter import LogNotificationAdapter
from app.adapters.notification.port import NotificationPort
from app.adapters.notification.webhook_adapter import WebhookNotificationAdapter
from app.core.config import settings
from app.schemas.notification import NotificationMessage

logger = structlog.get_logger("service.notification")


def _get_adapter() -> NotificationPort | None:
    """Resolve the active notification channel, or None when notifications are off."""
    if not settings.NOTIFICATIONS_ENABLED:
        return None
    if settings.NOTIFICATION_CHANNEL == "webhook" and settings.NOTIFICATION_WEBHOOK_URL:
        return WebhookNotificationAdapter(settings.NOTIFICATION_WEBHOOK_URL)
    return LogNotificationAdapter()


async def notificar(
    event: str,
    usuario_id: uuid.UUID,
    titulo: str,
    cuerpo: str,
    data: dict | None = None,
) -> bool:
    """Send a notification. Returns True if delivered, False if disabled or failed.

    Never raises — a failure here must not roll back or abort the caller.
    """
    adapter = _get_adapter()
    if adapter is None:
        return False

    message = NotificationMessage(
        event=event,
        usuario_id=usuario_id,
        titulo=titulo,
        cuerpo=cuerpo,
        data=data or {},
    )
    try:
        await adapter.send(message)
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open: notifications never break the flow
        await logger.awarning("notification_failed", event_key=event, error=str(exc))
        return False
