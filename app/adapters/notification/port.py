"""Notification port — abstraction over the outbound notification channel."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas.notification import NotificationMessage


class NotificationPort(ABC):
    """Sends a user-facing notification through a concrete channel."""

    @abstractmethod
    async def send(self, message: NotificationMessage) -> None:
        """Deliver the message. May raise on transport failure (caller decides policy)."""
