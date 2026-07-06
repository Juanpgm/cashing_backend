"""Google Calendar adapter — reads events as contractual evidence.

Reuses GmailAdapter for OAuth credential management (same GoogleToken row), so
all Google Workspace adapters share one token store with auto-refresh. All
Google API calls run via run_in_executor to stay non-blocking.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as GoogleHttpError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("adapters.calendar")


class GoogleCalendarAdapter:
    """Implements CalendarPort using the Google Calendar API v3 (DB-backed credentials)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._auth = GmailAdapter(db)  # credential management is shared

    def _build_service(self, creds):  # type: ignore[no-untyped-def]
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    async def search_events(
        self,
        usuario_id: uuid.UUID,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        max_results: int = 50,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """List events in the RFC3339 time range for the user's connected account.

        Args:
            q: Optional free-text search query (Google Calendar API ``q`` parameter).
               Use to bias results toward obligation-related events.

        Returns Google Calendar event resources (dicts) sorted by start time.
        """
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _list() -> dict:  # type: ignore[type-arg]
            params: dict[str, Any] = {
                "calendarId": calendar_id,
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": max_results,
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if q:
                params["q"] = q
            return service.events().list(**params).execute()

        try:
            result = await loop.run_in_executor(None, _list)
        except GoogleHttpError as exc:
            raise ExternalServiceError("Calendar", f"Error consultando eventos: {exc}") from exc
        items: list[dict[str, Any]] = result.get("items", [])
        logger.info("calendar_search", user_id=str(usuario_id), count=len(items), q=q)
        return items

    async def get_event(
        self,
        usuario_id: uuid.UUID,
        event_id: str,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """Fetch a single Google Calendar event by its ID."""
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _get() -> dict:  # type: ignore[type-arg]
            return service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        return await loop.run_in_executor(None, _get)
