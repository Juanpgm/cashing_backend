"""Webhook notification adapter — POSTs the notification as JSON to a configured URL."""

from __future__ import annotations

import httpx

from app.adapters.notification.port import NotificationPort
from app.schemas.notification import NotificationMessage


class WebhookNotificationAdapter(NotificationPort):
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout

    async def send(self, message: NotificationMessage) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=message.model_dump(mode="json"))
            resp.raise_for_status()
