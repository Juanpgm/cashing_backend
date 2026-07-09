"""Log notification adapter — the default/dev channel; writes structured logs."""

from __future__ import annotations

import structlog

from app.adapters.notification.port import NotificationPort
from app.schemas.notification import NotificationMessage

logger = structlog.get_logger("notification.log")


class LogNotificationAdapter(NotificationPort):
    async def send(self, message: NotificationMessage) -> None:
        await logger.ainfo(
            "notification",
            event_key=message.event,
            usuario_id=str(message.usuario_id),
            titulo=message.titulo,
            cuerpo=message.cuerpo,
            data=message.data,
        )
