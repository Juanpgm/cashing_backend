"""Calendar port — interface for Google Calendar operations."""

from __future__ import annotations

import uuid
from typing import Any, Protocol


class CalendarPort(Protocol):
    """Abstract interface for calendar event reading (DB-backed credentials)."""

    async def search_events(
        self,
        usuario_id: uuid.UUID,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """List calendar events in the given RFC3339 time range.

        Returns a list of Google Calendar event resources (dicts).
        """
        ...

    async def get_event(
        self,
        usuario_id: uuid.UUID,
        event_id: str,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """Fetch a single calendar event by ID."""
        ...
